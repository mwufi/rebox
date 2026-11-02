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
