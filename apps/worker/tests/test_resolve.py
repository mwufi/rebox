"""Resolver: hard keys, ambiguity -> review, scoped fuzzy, mint, batch cache."""

import pytest

from rebox_worker.ids import mint_id
from rebox_worker.models import ClaimInput, EntityRef, IdRef, MintRef
from rebox_worker.resolve import Resolver

LAYER = "public/test"


def company(name=None, ctx=None, **identity):
    return EntityRef(kind="company", identity=identity, name=name, context=ctx)


def person(name=None, ctx=None, **identity):
    return EntityRef(kind="person", identity=identity, name=name, context=ctx)


def ci(subject, predicate, value=None, obj=None, ref=None):
    return ClaimInput(
        subject=subject,
        predicate=predicate,
        value=value,
        object=obj,
        source_ref=ref or f"test:{predicate}:{value}:{id(subject)}",
    )


def seed(owner, eid, kind, *facts, created_at=None, layer="public/manual", merged_into=None):
    """Insert an entity + facts directly as the owner (test setup only)."""
    owner.execute(
        "insert into entity (id, kind, home_layer, created_at, merged_into) "
        "values (%s, %s, %s, coalesce(%s, now()), %s)",
        (eid, kind, layer, created_at, merged_into),
    )
    for pred, val, *obj in facts:
        owner.execute(
            "insert into claim (layer_id, subject, predicate, value, object_id, source_ref) "
            "values (%s, %s, %s, to_jsonb(%s::text), %s, %s)",
            (layer, eid, pred, val, obj[0] if obj else None, f"seed:{eid}:{pred}:{val}"),
        )


@pytest.fixture
def resolver(db):
    return Resolver(db, source="test", layer=LAYER)


def log_rows(owner):
    return owner.execute(
        "select decision, entity_id, confidence, evidence from resolution_log order by id"
    ).fetchall()


def test_hard_key_match_with_normalization(resolver, owner):
    seed(owner, "rbx:company:acme", "company", ("domain", "acme.ai"), ("name", "Acme"))
    res = resolver.resolve([ci(company(domain="https://www.ACME.ai/pricing"), "headcount", 12)])
    assert res.claims[0].subject == IdRef(id="rbx:company:acme")
    assert res.stats["matched"] == 1 and res.stats["minted"] == 0
    resolver.commit(res)
    [row] = log_rows(owner)
    assert row["decision"] == "matched" and row["confidence"] == 1.0
    assert row["evidence"][0]["pass"] == "hard_key"


def test_hard_key_follows_merged_into(resolver, owner):
    seed(owner, "rbx:company:target", "company", ("name", "Acme"))
    seed(owner, "rbx:company:old", "company", ("domain", "acme.ai"), merged_into="rbx:company:target")
    res = resolver.resolve([ci(company(domain="acme.ai"), "headcount", 3)])
    assert res.claims[0].subject == IdRef(id="rbx:company:target")


def test_hard_key_ignores_other_kinds(resolver, owner):
    seed(owner, "rbx:person:octo", "person", ("github", "octo"))
    res = resolver.resolve([ci(company(github="octo"), "headcount", 3)])
    assert isinstance(res.claims[0].subject, MintRef)


def test_ambiguous_match_opens_review_and_uses_oldest(resolver, owner):
    seed(owner, "rbx:company:new", "company", ("domain", "acme.ai"), created_at="2026-02-01")
    seed(owner, "rbx:company:old", "company", ("github", "acme"), created_at="2025-01-01")
    res = resolver.resolve([ci(company(domain="acme.ai", github="acme"), "headcount", 3)])
    assert res.claims[0].subject == IdRef(id="rbx:company:old")
    assert res.stats["review"] == 1
    resolver.commit(res)
    resolver.commit(resolver.resolve([ci(company(domain="acme.ai", github="acme"), "headcount", 3)]))
    reviews = owner.execute("select kind, entity_ids, status, reason from review_item").fetchall()
    assert len(reviews) == 1  # the second run does not open a duplicate review item
    assert reviews[0]["kind"] == "ambiguous_match" and reviews[0]["status"] == "open"
    assert reviews[0]["entity_ids"] == ["rbx:company:new", "rbx:company:old"]
    assert [r["decision"] for r in log_rows(owner)] == ["review", "review"]


def test_scoped_fuzzy_match(resolver, owner):
    seed(owner, "rbx:company:acme", "company", ("domain", "acme.ai"), ("name", "Acme"))
    seed(owner, "rbx:person:maya", "person", ("name", "Maya Chen"))
    owner.execute(
        "insert into claim (layer_id, subject, predicate, object_id, source_ref) "
        "values ('public/manual', 'rbx:person:maya', 'founded', 'rbx:company:acme', 'seed:f')"
    )
    # A same-named person with no edge to Acme must not be matched.
    seed(owner, "rbx:person:other-maya", "person", ("name", "Maya Chen"))

    ref = person(name="  maya CHEN. ", ctx=[company(domain="acme.ai")])
    res = resolver.resolve([ci(ref, "headline", "CEO")])
    assert res.claims[0].subject == IdRef(id="rbx:person:maya")
    resolver.commit(res)
    [row] = log_rows(owner)
    assert row["decision"] == "matched" and row["confidence"] == pytest.approx(0.8)
    assert row["evidence"][0]["pass"] == "scoped_fuzzy"

    # Without context, the same name is not enough: mint.
    res2 = resolver.resolve([ci(person(name="Maya Chen"), "headline", "CEO", ref="r2")])
    assert isinstance(res2.claims[0].subject, MintRef)


def test_mint_is_deterministic_and_adds_name(resolver):
    ref = company(name="Acme", domain="www.acme.ai")
    res = resolver.resolve([ci(ref, "headcount", 5, ref="test:acme:headcount")])
    expected = mint_id("company", {"domain": ["acme.ai"]}, "test", "ignored")
    assert res.claims[0].subject == MintRef(mint={"id": expected, "kind": "company"})
    names = [c for c in res.claims if c.predicate == "name"]
    assert len(names) == 1 and names[0].value == "Acme" and names[0].subject_id == expected
    assert res.stats == {"claims": 2, "matched": 0, "minted": 1, "review": 0, "rejected": 0}
    # Same input, new resolver: same id.
    again = Resolver(resolver.db, source="test", layer=LAYER).resolve([ci(ref, "headcount", 5)])
    assert again.claims[0].subject_id == expected


def test_mint_without_identity_is_scoped_to_record(resolver):
    a = resolver.resolve([("rec-1", ci(person(name="Bo"), "headline", "x"))]).claims[0].subject_id
    b = resolver.resolve([("rec-1", ci(person(name="Bo"), "headline", "y"))]).claims[0].subject_id
    c = resolver.resolve([("rec-2", ci(person(name="Bo"), "headline", "x"))]).claims[0].subject_id
    assert a == b != c


def test_existing_minted_id_is_reused_not_reminted(resolver, owner):
    eid = mint_id("person", {}, "test", "rec-1#person:bo")
    seed(owner, eid, "person", ("name", "Bo"))
    res = resolver.resolve([("rec-1", ci(person(name="Bo"), "headline", "x"))])
    assert res.claims[0].subject == IdRef(id=eid)
    assert res.stats["matched"] == 1 and res.stats["minted"] == 0


def test_batch_cache_consistency(resolver, owner):
    """One batch: the same entity described several ways resolves to one id, once."""
    acme_a = company(name="Acme", domain="acme.ai")
    acme_b = company(domain="https://www.acme.ai/", github="acme")  # same domain, new key
    acme_c = company(github="@Acme")  # only the key learned from acme_b
    ada = person(name="Ada", github="ada")
    res = resolver.resolve(
        [
            ci(acme_a, "headcount", 5, ref="1"),
            ci(acme_b, "founded_year", 2020, ref="2"),
            ci(acme_c, "yc_batch", "W24", ref="3"),
            ci(ada, "founded", obj=acme_c, ref="4"),
            ci(person(name="Ada", github="https://github.com/ADA"), "headline", "hi", ref="5"),
        ]
    )
    ids = {c.subject_id for c in res.claims if c.predicate in ("headcount", "founded_year", "yc_batch")}
    assert len(ids) == 1
    [acme_id] = ids
    founded = next(c for c in res.claims if c.predicate == "founded")
    assert founded.object_id == acme_id
    # Every reference to a not-yet-written entity is a mint ref (the writer reuses it if present).
    assert all(isinstance(c.subject, MintRef) for c in res.claims)
    assert res.stats["minted"] == 2 and res.stats["review"] == 0
    # Ada's two refs resolved to the same person.
    assert len({c.subject_id for c in res.claims if c.subject_id.startswith("rbx:person:")}) == 1
    # One generated name claim per minted entity.
    assert sorted(c.value for c in res.claims if c.predicate == "name") == ["Acme", "Ada"]


def test_batch_scoped_fuzzy_sees_unwritten_edges(resolver):
    acme = company(name="Acme", domain="acme.ai")
    res = resolver.resolve(
        [
            ("rec-1", ci(person(name="Bo Ex"), "founded", obj=acme, ref="1")),
            ("rec-2", ci(person(name="bo ex", ctx=[company(domain="acme.ai")]), "headline", "x", ref="2")),
        ]
    )
    bo1, bo2 = (c.subject_id for c in res.claims[:2])
    assert bo1 == bo2


def test_identity_claim_values_are_normalized(resolver):
    res = resolver.resolve([ci(company(domain="acme.ai"), "domain", "https://www.Acme.ai/x")])
    assert res.claims[0].value == "acme.ai"


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        (ci(person(email="a@b.co"), "email", "a@b.co"), "private"),
        (ci(company(domain="a.co"), "nope", 1), "unknown predicate"),
        (ci(company(domain="a.co"), "headline", "x"), "subject kind"),
        (ci(person(github="a"), "founded", obj=person(github="b")), "company object"),
        (ci(company(domain="a.co"), "headcount", None), "needs a value"),
        (ci(company(domain="a.co"), "domain", "localhost"), "not normalizable"),
    ],
)
def test_invalid_claims_are_rejected_not_sent(resolver, claim, why):
    res = resolver.resolve([claim, ci(company(name="Ok", domain="ok.co"), "headcount", 1, ref="ok")])
    assert len(res.rejected) == 1 and why in res.rejected[0][1]
    assert [c.predicate for c in res.claims] == ["headcount", "name"]


def test_hard_key_lookup_query(db, owner):
    """Pass 1's batch query returns exactly the visible, live identity claims that match.

    Behaviour, not plan: the planner's choice depends on table statistics (on an empty table
    it may pick any index), so this checks results on a realistically sized, analyzed table.
    """
    from rebox_worker.resolve import HARD_KEY_SQL

    owner.execute("insert into layer (id, scope, owner, source) values ('user/zz/crm', 'user', 'zz', 'crm')")
    owner.execute(
        "insert into entity (id, kind, home_layer) "
        "select 'rbx:company:c' || g, 'company', 'public/manual' from generate_series(1, 5000) g"
    )
    owner.execute(
        "insert into claim (layer_id, subject, predicate, value, source_ref) "
        "select 'public/manual', 'rbx:company:c' || g, p, to_jsonb(p || '-' || g), 'seed:' || g "
        "from generate_series(1, 5000) g, unnest(array['domain', 'name', 'github']) p"
    )
    # A retracted claim, a claim in a non-visible layer and a row with an object: no hits.
    owner.execute(
        "insert into claim (layer_id, subject, predicate, value, source_ref, retract, object_id) values"
        " ('public/manual', 'rbx:company:c8', 'domain', '\"gone.example\"', 'r', true, null),"
        " ('user/zz/crm', 'rbx:company:c9', 'github', '\"private-gh\"', 'p', false, null),"
        " ('public/manual', 'rbx:company:c10', 'domain', '\"edge.example\"', 'e', false, 'rbx:company:c11')"
    )
    # A second (public) entity with the same domain: both are returned.
    owner.execute(
        "insert into claim (layer_id, subject, predicate, value, source_ref)"
        " values ('public/manual', 'rbx:company:c12', 'domain', '\"domain-7\"', 'dup')"
    )
    owner.execute("analyze claim")
    preds = ["domain", "github", "domain", "github", "domain", "domain", "github"]
    values = ["domain-7", "github-1", "gone.example", "private-gh", "edge.example", "name-3", "nope"]
    with db.view() as conn:
        hits = conn.execute(HARD_KEY_SQL, (preds, values)).fetchall()
    assert sorted((h["predicate"], h["value"], h["subject"]) for h in hits) == [
        ("domain", "domain-7", "rbx:company:c12"),
        ("domain", "domain-7", "rbx:company:c7"),
        ("github", "github-1", "rbx:company:c1"),
    ]


def test_only_identity_predicates_are_hard_keys(resolver, owner):
    seed(owner, "rbx:company:acme", "company", ("yc_batch", "W24"))
    res = resolver.resolve([ci(company(name="Other", yc_batch="W24"), "headcount", 1)])
    assert isinstance(res.claims[0].subject, MintRef)  # yc_batch is not an identity key
    resolver.commit(res)
    [row] = owner.execute("select identity from resolution_log").fetchall()
    assert row["identity"]["invalid"][0]["predicate"] == "yc_batch"


def _seed_bulk(owner, n=5000):
    owner.execute(
        "insert into entity (id, kind, home_layer) "
        "select 'rbx:company:c' || g, 'company', 'public/manual' from generate_series(1, %s) g",
        (n,),
    )
    owner.execute(
        "insert into claim (layer_id, subject, predicate, value, source_ref) "
        "select 'public/manual', 'rbx:company:c' || g, p, to_jsonb(p || '-' || g), 'seed:' || g "
        "from generate_series(1, %s) g, unnest(array['domain', 'name', 'github', 'x_handle']) p",
        (n,),
    )
    owner.execute("analyze claim")


def _plan_nodes(plan) -> list[dict]:
    out: list[dict] = []
    if isinstance(plan, dict):
        out.append(plan)
        for v in plan.values():
            out += _plan_nodes(v)
    elif isinstance(plan, list):
        for v in plan:
            out += _plan_nodes(v)
    return out


def test_hard_key_query_plan_uses_value_text_index_under_rls(db, owner):
    """As rebox_app (RLS on), on 20k analyzed claims, pass 1 is an index lookup on
    claim_value_text_idx with value_text in the index condition (CONTRACTS §2a)."""
    from psycopg import ClientCursor

    from rebox_worker.resolve import HARD_KEY_SQL

    _seed_bulk(owner)
    assert owner.execute("select count(*) n from claim").fetchone()["n"] >= 10_000
    preds = ["domain", "github", "domain"]
    values = ["domain-7", "github-1", "domain-4242"]
    with db.view() as conn:
        assert conn.execute("select current_user u").fetchone()["u"] == "rebox_app"
        cur = ClientCursor(conn)
        cur.execute("explain (format json) " + HARD_KEY_SQL, (preds, values))
        plan = cur.fetchone()["QUERY PLAN"]
        hits = conn.execute(HARD_KEY_SQL, (preds, values)).fetchall()
    nodes = _plan_nodes(plan)
    scans = [n for n in nodes if n.get("Relation Name") == "claim"]
    assert scans and all(n.get("Index Name") == "claim_value_text_idx" for n in scans), plan
    # Both columns are index conditions (not post-RLS filters): a true lookup.
    assert all("value_text = ANY" in n.get("Index Cond", "") for n in scans), plan
    assert all("predicate = " in n.get("Index Cond", "") for n in scans), plan
    assert sorted((h["predicate"], h["value"]) for h in hits) == [
        ("domain", "domain-4242"),
        ("domain", "domain-7"),
        ("github", "github-1"),
    ]


# ── S16: unscoped company name pass, name mints, x_id ─────────────────────


def name_mint(name):
    from rebox_worker.names import fold_name

    return mint_id("company", {"name": fold_name("company", name)}, "ignored", "ignored")


def subject_of(res, predicate):
    return next(c.subject_id for c in res.claims if c.predicate == predicate)


def test_two_records_naming_a_company_converge(resolver, writer, client, owner):
    """'SWE at Google' in two bios -> one company, in one batch and across batches."""
    res = resolver.resolve(
        [
            (
                "rec-1",
                ci(person(name="Ada", x_handle="ada"), "worked_at", obj=company(name="Google"), ref="1"),
            ),
            (
                "rec-2",
                ci(person(name="Bo", x_handle="bo"), "worked_at", obj=company(name="Google LLC"), ref="2"),
            ),
        ]
    )
    g1, g2 = (c.object_id for c in res.claims if c.predicate == "worked_at")
    assert g1 == g2 == name_mint("Google")
    assert res.stats["minted"] == 3  # Ada, Bo, Google
    resolver.commit(res)
    rows = owner.execute(
        "select decision, evidence from resolution_log where kind = 'company' order by id"
    ).fetchall()
    assert rows[0]["decision"] == "minted" and rows[0]["evidence"][0] == {
        "pass": "mint",
        "basis": "name",
        "name": "google",
    }
    assert rows[1]["decision"] == "matched" and rows[1]["evidence"][0]["pass"] == "company_name"

    # Written, then a later batch (another record, another resolver): the same entity, matched.
    w = client.register_worker(name="t", kind="connector", source="t", layers=[LAYER])
    client.write(LAYER, res.claims, worker=w)
    later = Resolver(resolver.db, source="other", layer=LAYER).resolve(
        [("rec-3", ci(person(name="Cy", x_handle="cy"), "worked_at", obj=company(name="google"), ref="3"))]
    )
    worked_at = next(c for c in later.claims if c.predicate == "worked_at")
    assert worked_at.object == IdRef(id=g1)
    assert owner.execute("select count(*) n from entity where kind = 'company'").fetchone()["n"] == 1


def test_name_only_company_matches_keyed_company(resolver, owner):
    seed(owner, "rbx:company:goog", "company", ("wikidata", "Q95"), ("name", "Google LLC"))
    seed(owner, "rbx:company:goog-labs", "company", ("domain", "labs.google"), ("name", "Google Labs"))
    res = resolver.resolve([ci(company(name="Google"), "headcount", 5)])
    assert res.claims[0].subject == IdRef(id="rbx:company:goog")
    assert res.stats == {"claims": 1, "matched": 1, "minted": 0, "review": 0, "rejected": 0}
    resolver.commit(res)
    [row] = log_rows(owner)
    assert row["decision"] == "matched" and row["confidence"] == pytest.approx(0.7)
    assert row["evidence"] == [
        {
            "pass": "company_name",
            "name": "google",
            "entity": "rbx:company:goog",
            "keyed": True,
            "safe": True,
            "claims": 2,
        }
    ]


def test_keyed_beats_name_only_and_name_variants_count(resolver, owner):
    seed(owner, "rbx:company:a-name-only", "company", ("name", "Palantir"), ("headcount", "9"))
    # The keyed one is newer and has fewer claims, and search_name holds only its last-written
    # name in the layer ("Palantir Technologies Inc."): its other name claims count too.
    seed(
        owner,
        "rbx:company:z-keyed",
        "company",
        ("name", "Palantir"),
        ("x_handle", "palantirtech"),
        ("domain", "palantir.com"),  # safe: its domain label is the name
        ("name", "Palantir Technologies Inc."),
        created_at="2030-01-01",
    )
    assert (
        owner.execute("select name from search_name where entity_id = 'rbx:company:z-keyed'").fetchone()[
            "name"
        ]
        == "Palantir Technologies Inc."
    )
    res = resolver.resolve([ci(company(name="Palantir, Inc."), "headcount", 5)])
    assert res.claims[0].subject == IdRef(id="rbx:company:z-keyed")


def test_several_keyed_companies_use_oldest_and_open_one_review(resolver, owner):
    seed(
        owner,
        "rbx:company:notion-new",
        "company",
        ("domain", "notion.so"),
        ("name", "Notion"),
        created_at="2026-02-01",
    )
    seed(
        owner,
        "rbx:company:notion-old",
        "company",
        ("wikidata", "Q89269703"),
        ("name", "Notion, Inc."),
        created_at="2025-01-01",
    )
    seed(owner, "rbx:company:notion-bare", "company", ("name", "Notion"), created_at="2024-01-01")
    items = [
        ci(company(name="Notion"), "headcount", 5, ref="1"),
        ci(company(name="Notion Inc"), "yc_batch", "S20", ref="2"),
    ]
    res = resolver.resolve(items)
    assert {c.subject_id for c in res.claims} == {"rbx:company:notion-old"}  # oldest keyed, not oldest
    # The second ref (another spelling) reuses the batch decision: matched, no second review.
    assert res.stats["review"] == 1 and res.stats["matched"] == 1 and len(res.reviews) == 1
    resolver.commit(res)
    resolver.commit(resolver.resolve(items))  # a second run opens no second item
    reviews = owner.execute("select id, kind, entity_ids, status, reason from review_item").fetchall()
    assert len(reviews) == 1
    assert reviews[0]["kind"] == "possible_duplicate" and reviews[0]["status"] == "open"
    assert reviews[0]["entity_ids"] == ["rbx:company:notion-new", "rbx:company:notion-old"]
    assert "'notion'" in reviews[0]["reason"]
    # Dismissed stays dismissed ("once per name set").
    owner.execute("update review_item set status = 'dismissed' where id = %s", (reviews[0]["id"],))
    resolver.commit(resolver.resolve(items))
    assert owner.execute("select count(*) n from review_item").fetchone()["n"] == 1


def test_name_only_candidates_pick_most_claims_then_oldest(resolver, owner):
    seed(owner, "rbx:company:ramp-a", "company", ("name", "Mercury"), created_at="2026-01-01")
    seed(
        owner,
        "rbx:company:ramp-b",
        "company",
        ("name", "Mercury"),
        ("headcount", "3"),
        ("yc_batch", "W19"),
        created_at="2026-03-01",
    )
    seed(
        owner,
        "rbx:company:ramp-c",
        "company",
        ("name", "mercury"),
        ("headcount", "4"),
        ("yc_batch", "W19"),
        created_at="2026-02-01",
    )
    res = resolver.resolve([ci(company(name="Mercury"), "headcount", 5)])
    assert res.claims[0].subject == IdRef(id="rbx:company:ramp-c")  # 3 claims, tie with b, older
    resolver.commit(res)
    [row] = log_rows(owner)
    assert row["decision"] == "matched" and row["confidence"] == pytest.approx(0.6)
    assert owner.execute("select count(*) n from review_item").fetchone()["n"] == 0


def test_name_pass_follows_merges_and_skips_other_kinds(resolver, owner):
    seed(owner, "rbx:company:stripe", "company", ("wikidata", "Q7624104"), ("name", "Stripe"))
    seed(
        owner, "rbx:company:stripe-dup", "company", ("name", "Stripe, Inc."), merged_into="rbx:company:stripe"
    )
    seed(owner, "rbx:person:stripe", "person", ("x_handle", "stripe_person"), ("name", "Stripe Inc"))
    res = resolver.resolve([ci(company(name="STRIPE INC"), "headcount", 5)])
    assert res.claims[0].subject == IdRef(id="rbx:company:stripe")
    resolver.commit(res)
    [row] = log_rows(owner)
    assert [e["entity"] for e in row["evidence"]] == ["rbx:company:stripe"]


def test_persons_are_never_matched_by_name(resolver, owner):
    seed(owner, "rbx:person:ada", "person", ("x_handle", "ada"), ("name", "Ada Lovelace"))
    a = resolver.resolve([("rec-1", ci(person(name="Ada Lovelace"), "headline", "x"))])
    b = resolver.resolve([("rec-2", ci(person(name="Ada Lovelace"), "headline", "x"))])
    assert isinstance(a.claims[0].subject, MintRef) and isinstance(b.claims[0].subject, MintRef)
    assert a.claims[0].subject_id != b.claims[0].subject_id  # still scoped to the record
    assert a.claims[0].subject_id == mint_id("person", {}, "test", "rec-1#person:ada lovelace")


def test_placeholder_company_names_stay_per_record(resolver, owner):
    seed(owner, "rbx:company:stealth", "company", ("domain", "stealth.example"), ("name", "Stealth"))
    a = resolver.resolve([("rec-1", ci(company(name="Stealth"), "headcount", 1))]).claims[0]
    b = resolver.resolve([("rec-2", ci(company(name="Stealth Startup"), "headcount", 1))]).claims[0]
    assert isinstance(a.subject, MintRef) and isinstance(b.subject, MintRef)
    assert a.subject_id != b.subject_id != "rbx:company:stealth"


def test_name_pass_uses_keyed_companies_resolved_earlier_in_the_batch(resolver):
    res = resolver.resolve(
        [
            ("rec-1", ci(company(name="Acme", domain="acme.ai"), "headcount", 5, ref="1")),
            ("rec-2", ci(company(name="Acme, Inc."), "yc_batch", "W24", ref="2")),
        ]
    )
    keyed = mint_id("company", {"domain": "acme.ai"}, "", "")
    assert [c.subject for c in res.claims if c.predicate != "name"] == [
        MintRef(mint={"id": keyed, "kind": "company"}),
        MintRef(mint={"id": keyed, "kind": "company"}),
    ]
    assert res.stats["minted"] == 1 and res.stats["matched"] == 1


def test_scoped_fuzzy_runs_before_the_name_pass(resolver, owner):
    seed(owner, "rbx:company:acme-keyed", "company", ("domain", "acme.ai"), ("name", "Acme"))
    seed(owner, "rbx:company:acme-local", "company", ("name", "Acme"))
    seed(owner, "rbx:person:maya", "person", ("x_handle", "maya"), ("name", "Maya"))
    owner.execute(
        "insert into claim (layer_id, subject, predicate, object_id, source_ref) "
        "values ('public/manual', 'rbx:person:maya', 'founded', 'rbx:company:acme-local', 'seed:f')"
    )
    res = resolver.resolve([ci(company(name="Acme", ctx=[person(x_handle="maya")]), "headcount", 5)])
    assert res.claims[0].subject == IdRef(id="rbx:company:acme-local")


def test_existing_name_minted_company_is_reused(resolver, owner):
    eid = name_mint("Vori Health")
    seed(owner, eid, "company")  # exists, but its name claim is not visible (e.g. not written yet)
    res = resolver.resolve([("rec-9", ci(company(name="Vori Health"), "headcount", 1))])
    assert res.claims[0].subject == IdRef(id=eid)


def test_name_candidate_query_uses_trigram_index(db, owner):
    """Pass 3 candidates come from search_name through its GIN trigram index (no RLS there)."""
    from psycopg import ClientCursor

    from rebox_worker.resolve import NAME_CANDIDATE_LIMIT, name_candidates_sql

    _seed_bulk(owner)
    owner.execute(
        "insert into claim (layer_id, subject, predicate, value, source_ref) values "
        "('public/manual', 'rbx:company:c77', 'name', '\"Google LLC\"', 'g')"
    )
    # Bulk-inserted names sit in the GIN pending list until vacuum, which the planner prices
    # as a full scan; a live table is vacuumed by autovacuum.
    owner.execute("vacuum analyze search_name")
    one = name_candidates_sql(1, NAME_CANDIDATE_LIMIT)
    with db.view() as conn:
        cur = ClientCursor(conn)
        cur.execute("explain (format json) " + one, ["google"] * 3)
        plan = cur.fetchone()["QUERY PLAN"]
        two = name_candidates_sql(2, NAME_CANDIDATE_LIMIT)
        hits = conn.execute(two, ["google"] * 3 + ["name-42"] * 3).fetchall()
    assert any(n.get("Index Name") == "search_name_trgm_idx" for n in _plan_nodes(plan)), plan
    found = {(h["folded"], h["entity_id"]) for h in hits}
    assert ("google", "rbx:company:c77") in found and ("name-42", "rbx:company:c42") in found


def test_x_id_only_refs_in_one_record_are_distinct(resolver):
    """x_id is an identity key: two accounts known only by x_id never collapse into one."""
    res = resolver.resolve(
        [
            ("rec-1", ci(person(x_id="111"), "follows", obj=person(x_id="222"), ref="1")),
            ("rec-1", ci(person(x_id="222"), "headline", "hi", ref="2")),
        ]
    )
    follows, headline = res.claims
    assert follows.subject_id != follows.object_id
    assert follows.subject_id == mint_id("person", {"x_id": "111"}, "", "")
    assert headline.subject_id == follows.object_id
    assert res.stats["minted"] == 2


def test_x_id_ranks_after_x_handle():
    from rebox_worker.ids import IDENTITY_KEY_ORDER, MINT_KEY_ORDER, strongest_key

    assert IDENTITY_KEY_ORDER.index("x_id") == IDENTITY_KEY_ORDER.index("x_handle") + 1
    assert strongest_key({"x_id": "1", "x_handle": "a"}) == ("x_handle", "a")
    assert strongest_key({"x_id": "1", "hn_user": "a"}) == ("x_id", "1")
    assert MINT_KEY_ORDER[-1] == "name" and strongest_key({"name": "acme"}) is None
    # ids minted from stronger keys are unchanged by the new entry
    assert mint_id("company", {"x_handle": "a", "x_id": "1"}, "s", "r") == mint_id(
        "company", {"x_handle": "a"}, "s", "r"
    )
    assert mint_id("company", {"x_id": "1", "name": "acme"}, "s", "r") == mint_id(
        "company", {"x_id": "1"}, "", ""
    )


# ── S16 addendum: keyed targets must be safe; short names do not converge ──


def test_one_keyed_but_unsafe_falls_through_to_the_name_path(resolver, owner):
    """'Capital' (x_handle only, few in-edges) is not a safe target: a name mint instead."""
    seed(owner, "rbx:company:capital-x", "company", ("x_handle", "capital_xyz"), ("name", "Capital"))
    res = resolver.resolve([("rec-1", ci(company(name="Capital"), "headcount", 1))])
    assert res.claims[0].subject_id == name_mint("Capital")  # 7 chars: converges by name
    assert res.stats["review"] == 0 and not res.reviews
    # ... and a name-only "Capital" is preferred over the unsafe keyed one.
    seed(owner, "rbx:company:capital-bare", "company", ("name", "Capital"))
    res = resolver.resolve([("rec-2", ci(company(name="Capital"), "headcount", 1))])
    assert res.claims[0].subject == IdRef(id="rbx:company:capital-bare")


def test_safe_by_domain_label_including_two_part_suffixes(resolver, owner):
    seed(
        owner,
        "rbx:company:ramp",
        "company",
        ("x_handle", "tryramp"),
        ("domain", "ramp.com"),
        ("name", "Ramp"),
    )
    seed(owner, "rbx:company:monzo", "company", ("domain", "monzo.co.uk"), ("name", "Monzo Bank Ltd"))
    seed(owner, "rbx:company:coke", "company", ("domain", "coca-cola.com"), ("name", "Coca-Cola Co."))
    res = resolver.resolve(
        [
            ci(company(name="Ramp"), "headcount", 1, ref="1"),  # short, but a safe keyed target
            ci(company(name="Monzo Bank"), "headcount", 1, ref="2"),  # label "monzo" != "monzobank"
            ci(company(name="Coca-Cola"), "headcount", 1, ref="3"),  # punctuation ignored
        ]
    )
    ramp, monzo, coke = (c.subject for c in res.claims if c.predicate == "headcount")
    assert ramp == IdRef(id="rbx:company:ramp") and coke == IdRef(id="rbx:company:coke")
    assert monzo == MintRef(mint={"id": name_mint("Monzo Bank"), "kind": "company"})


def _in_edges(owner, target, n):
    for i in range(n):
        seed(owner, f"rbx:person:fan{target[-3:]}{i}", "person", ("name", f"Fan {i}"))
        owner.execute(
            "insert into claim (layer_id, subject, predicate, object_id, source_ref) "
            "values ('public/manual', %s, 'worked_at', %s, %s)",
            (f"rbx:person:fan{target[-3:]}{i}", target, f"seed:w:{target}:{i}"),
        )


def test_safe_by_incoming_edges_from_distinct_subjects(resolver, owner):
    seed(owner, "rbx:company:balance-hub", "company", ("x_handle", "balancehq"), ("name", "Balance"))
    seed(owner, "rbx:company:extend-few", "company", ("x_handle", "extendhq"), ("name", "Extend"))
    _in_edges(owner, "rbx:company:balance-hub", 5)
    _in_edges(owner, "rbx:company:extend-few", 4)
    # follows edges never count
    for i in range(5):
        owner.execute(
            "insert into claim (layer_id, subject, predicate, object_id, source_ref) "
            "values ('public/manual', %s, 'follows', 'rbx:company:extend-few', %s)",
            (f"rbx:person:fanhub{i}", f"seed:f:{i}"),
        )
    res = resolver.resolve(
        [
            ("rec-1", ci(company(name="Balance"), "headcount", 1, ref="1")),
            ("rec-2", ci(company(name="Extend"), "headcount", 1, ref="2")),
        ]
    )
    balance, extend = (c.subject for c in res.claims if c.predicate == "headcount")
    assert balance == IdRef(id="rbx:company:balance-hub")
    assert extend == MintRef(mint={"id": name_mint("Extend"), "kind": "company"})


def test_short_names_never_converge_without_a_safe_target(resolver, owner):
    """'Base' (one token, < 6 chars): no name-only match, no name mint -> record-scoped."""
    seed(owner, "rbx:company:base-keyed", "company", ("x_handle", "base"), ("name", "Base"))
    seed(owner, "rbx:company:base-bare", "company", ("name", "Base"), ("headcount", "3"))
    a = resolver.resolve([("rec-1", ci(company(name="Base"), "headcount", 1))])
    b = resolver.resolve([("rec-2", ci(company(name="Base"), "headcount", 1))])
    assert a.claims[0].subject_id == mint_id("company", {}, "test", "rec-1#company:base")
    assert b.claims[0].subject_id == mint_id("company", {}, "test", "rec-2#company:base")
    resolver.commit(a)
    [row] = log_rows(owner)
    assert row["decision"] == "minted" and row["evidence"][0]["basis"] == "source_ref"
    # Two tokens are specific enough: "Base Camp" converges.
    c = resolver.resolve([("rec-3", ci(company(name="Base Camp"), "headcount", 1))])
    assert c.claims[0].subject_id == name_mint("Base Camp")


def test_several_keyed_none_safe_falls_through_without_review(resolver, owner):
    seed(owner, "rbx:company:cmp-a", "company", ("x_handle", "compound_a"), ("name", "Compound"))
    seed(owner, "rbx:company:cmp-b", "company", ("github", "compound-b"), ("name", "Compound"))
    res = resolver.resolve([("rec-1", ci(company(name="Compound"), "headcount", 1))])
    assert res.claims[0].subject_id == name_mint("Compound")
    assert not res.reviews and res.stats["review"] == 0
    seed(owner, "rbx:company:cmp-bare", "company", ("name", "Compound"))
    res = resolver.resolve([("rec-2", ci(company(name="Compound"), "headcount", 1))])
    assert res.claims[0].subject == IdRef(id="rbx:company:cmp-bare") and not res.reviews


def test_several_keyed_one_safe_uses_it_and_reviews_all_keyed(resolver, owner):
    seed(
        owner,
        "rbx:company:fig-old",
        "company",
        ("x_handle", "figma_fan"),
        ("name", "Figma"),
        created_at="2020-01-01",
    )
    seed(
        owner,
        "rbx:company:fig-new",
        "company",
        ("domain", "figma.com"),
        ("name", "Figma"),
        created_at="2026-01-01",
    )
    res = resolver.resolve([ci(company(name="Figma, Inc."), "headcount", 1)])
    assert res.claims[0].subject == IdRef(id="rbx:company:fig-new")  # the oldest *safe* one
    [rv] = res.reviews
    assert rv.kind == "possible_duplicate" and rv.entity_ids == ["rbx:company:fig-new", "rbx:company:fig-old"]


def test_batch_local_keyed_targets_are_guarded_too(resolver):
    res = resolver.resolve(
        [
            ("rec-1", ci(company(name="Plaid", domain="plaid.com"), "headcount", 5, ref="1")),
            ("rec-2", ci(company(name="Plaid"), "yc_batch", "W13", ref="2")),
            ("rec-3", ci(company(name="Anchor", x_handle="anchor_x"), "headcount", 5, ref="3")),
            ("rec-4", ci(company(name="Anchor"), "yc_batch", "W14", ref="4")),
        ]
    )
    subjects = [c.subject_id for c in res.claims if c.predicate in ("headcount", "yc_batch")]
    assert subjects[0] == subjects[1] == mint_id("company", {"domain": "plaid.com"}, "", "")
    assert subjects[2] == mint_id("company", {"x_handle": "anchor_x"}, "", "")
    assert subjects[3] == name_mint("Anchor")  # 6 chars: converges, but not onto the unsafe keyed one
