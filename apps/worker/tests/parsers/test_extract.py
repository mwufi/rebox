# ruff: noqa: E501  (inline synthetic payloads read better on one line)
"""extract (pure): recorded model outputs -> evidence-backed claims, and every guard."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import load_recorded

from rebox_worker.models import RawRecord
from rebox_worker.parsers.extract import extract_claims, has_private_data
from rebox_worker.parsers.inputs import doc_for
from rebox_worker.parsers.places import canonical_city
from rebox_worker.parsers.prompt import PROMPT_VERSION, InputDoc
from rebox_worker.parsers.schema import INPUT_SCHEMA, TOOL, parse_facts

FETCHED = datetime(2026, 10, 25, tzinfo=UTC)


def _doc(source: str, payload: dict[str, Any]) -> InputDoc:
    doc = doc_for(RawRecord.new(source, f"t:{abs(hash(str(payload))) % 10**8}", payload, fetched_at=FETCHED))
    assert doc is not None
    return doc


def parsed(
    doc: InputDoc, output: dict[str, Any] | None, graph: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "input_ref": doc.ref,
        "input_hash": "",
        "model": "claude-haiku-5-5",
        "prompt_version": PROMPT_VERSION,
        "input": {"kind": doc.kind, "fields": doc.fields, "meta": doc.meta},
        "output": output,
        "graph": graph or {},
    }


def run(
    source: str, payload: dict[str, Any], facts: list[dict[str, Any]], account: str = "person", graph=None
):
    doc = _doc(source, payload)
    return extract_claims("x_parse", parsed(doc, {"account": account, "facts": facts}, graph))


def _ev(c) -> dict[str, Any]:
    e = c.evidence[0]
    return e if isinstance(e, dict) else e.model_dump(exclude_none=True)


def _text_of(c) -> str:
    """Everything human-readable a claim carries: value, names, evidence quotes (not ids)."""
    if c.predicate in ("x_id", "x_handle", "github"):
        parts = []
    elif isinstance(c.value, dict):  # props / milestone: text fields, not the post URL (tweet ids)
        parts = [str(v) for k, v in c.value.items() if k != "url"]
    else:
        parts = [str(c.value or "")]
    parts += [ref.name for ref in (c.subject, c.object) if ref is not None and ref.name]
    if c.evidence:
        parts.append(str(_ev(c).get("quote") or ""))
    return "\n".join(parts)


def by_pred(claims, predicate: str) -> list:
    return [c for c in claims if c.predicate == predicate]


def recorded_claims(path: str, account_kinds: dict[str, str] | None = None) -> dict[str, list]:
    """Claims per input text for a recorded fixture file; ``account_kinds`` = what the graph knows."""
    data = load_recorded(path)
    out = {}
    for item in data["inputs"]:
        doc = _doc(item["source"], item["payload"])
        graph = {}
        h = str(doc.meta.get("handle") or "").lower()
        if account_kinds and h in account_kinds:
            graph[f"x_handle:{h}"] = [account_kinds[h]]
        out[doc.text()] = extract_claims("x_parse", parsed(doc, data["outputs"][doc.text()], graph))
    return out


def find(claims_by_text: dict[str, list], needle: str) -> list:
    hits = [v for k, v in claims_by_text.items() if needle in k]
    assert len(hits) == 1, needle
    return hits[0]


# ── schema ──


def test_tool_schema_is_strict_compatible():
    """Strict tool use: every object closes additionalProperties and requires all its keys."""
    assert TOOL["strict"] is True and TOOL["input_schema"] is INPUT_SCHEMA

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for bad in ("minLength", "maxLength", "minimum", "maximum", "maxItems"):
                assert bad not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(INPUT_SCHEMA)


def test_schema_validation_drops_malformed_facts():
    facts, bad = parse_facts(
        [
            {"type": "role", "org": "Acme", "quote": "at Acme"},
            {"type": "role", "org": "Acme"},  # no quote
            {"type": "location", "place": "", "quote": "SF"},  # empty place
            {"type": "spaceship", "quote": "x"},  # unknown type
            "not an object",
            {"type": "milestone", "kind": "Launch", "title": "Launched X", "quote": "launching X"},
            {"type": "milestone", "kind": "party", "title": "Party", "quote": "party"},
        ]
    )
    assert bad == 4
    assert [f.type for f in facts] == ["role", "milestone", "milestone"]
    assert facts[1].kind == "launch" and facts[2].kind == "other"  # casing drift, unknown kind
    assert parse_facts(None) == ([], 0)


def test_null_output_gives_no_claims():
    doc = _doc("x_spider", load_recorded("x_parse/bios.json")["inputs"][0]["payload"])
    assert extract_claims("x_parse", parsed(doc, None)) == []


# ── the hallucination guard ──


def test_evidence_substring_guard_drops_fabricated_quotes():
    payload = load_recorded("x_parse/bios.json")["inputs"][0]["payload"]  # Zoe Hart
    claims = run(
        "x_spider",
        payload,
        [
            {"type": "role", "org": "Tally", "org_handle": "tallyhq_ex", "title": "CEO", "status": "current",
             "founder": True, "investor": False, "quote": "co-founder & CEO @tallyhq_ex"},
            # fabricated: not in the bio
            {"type": "role", "org": "Google", "org_handle": "", "title": "SWE", "status": "past",
             "founder": False, "investor": False, "quote": "ex-Google SWE"},
            # paraphrased: case changed
            {"type": "location", "place": "San Francisco", "quote": "san francisco, ca"},
        ],
    )  # fmt: skip
    objs = {(c.predicate, (c.object.identity or {}).get("x_handle") if c.object else None) for c in claims}
    assert ("worked_at", "tallyhq_ex") in objs and ("founded", "tallyhq_ex") in objs
    assert not by_pred(claims, "based_in")
    assert not any(c.object and c.object.name == "Google" for c in claims)
    for c in claims:  # every claim carries evidence whose quote is in the input
        ev = _ev(c)
        assert ev["url"] == "https://x.com/zoe_founder_ex" and ev["author"] == "@zoe_founder_ex"
        assert (
            ev["quote"]
            in payload["user"]["description"] + payload["user"]["location"] + payload["user"]["name"]
        )


def test_label_prefixed_quote_is_accepted():
    payload = load_recorded("x_parse/bios.json")["inputs"][1]["payload"]  # Marc, location "SF"
    claims = run(
        "x_spider", payload, [{"type": "location", "place": "San Francisco", "quote": "location: SF"}]
    )
    (b,) = by_pred(claims, "based_in")
    assert b.object.identity == {"wikidata": "Q62"}


def test_org_names_and_handles_must_be_in_the_text():
    payload = load_recorded("x_parse/bios.json")["inputs"][1]["payload"]
    claims = run(
        "x_spider",
        payload,
        [
            {"type": "role", "org": "Harbor Capital", "org_handle": "", "title": "", "status": "current",
             "founder": False, "investor": True, "quote": "investing at @harborvc_ex"},
            {"type": "role", "org": "", "org_handle": "loopy_ex", "title": "eng", "status": "past",
             "founder": False, "investor": False, "quote": "formerly eng @looptech_ex"},
        ],
    )  # fmt: skip
    assert claims == []


# ── bios ──


def test_handle_org_becomes_company_ref_with_x_handle_identity():
    claims = find(recorded_claims("x_parse/bios.json"), "Marc Bell")
    (w,) = [c for c in by_pred(claims, "worked_at") if c.valid_to is None]
    assert w.object.kind == "company" and w.object.identity == {"x_handle": "harborvc_ex"}
    assert w.value == {"title": "Investor"}  # "investing at @x" -> investor role
    # the unknown company also gets a name claim with evidence (so the resolver adds none)
    names = [c for c in by_pred(claims, "name") if c.subject.identity == {"x_handle": "harborvc_ex"}]
    assert names and names[0].evidence


def test_ex_and_prev_become_ended_roles():
    claims = find(recorded_claims("x_parse/bios.json"), "Zoe Hart")
    ended = {
        (c.object.identity.get("x_handle") or c.object.name): c
        for c in by_pred(claims, "worked_at")
        if c.valid_to
    }
    assert set(ended) == {"stripe_ex", "Plaidly"}
    assert all(c.valid_to == "2026-10-25" for c in ended.values())  # observed date
    assert ended["Plaidly"].value == {"title": "PM"}
    (current,) = [c for c in by_pred(claims, "worked_at") if c.valid_to is None]
    assert current.value == {"title": "CEO"}  # "co-founder & CEO" -> founded + CEO
    (f,) = by_pred(claims, "founded")
    assert f.object.identity == {"x_handle": "tallyhq_ex"}
    # a name-only company is scoped to the person for the resolver's fuzzy pass
    assert ended["Plaidly"].object.context[0].identity["x_handle"] == "zoe_founder_ex"


@pytest.mark.parametrize(
    ("text", "city", "qid"),
    [
        ("SF", "San Francisco", "Q62"),
        ("San Francisco, CA", "San Francisco", "Q62"),
        ("Bay Area", "San Francisco", "Q62"),
        ("sf bay area", "San Francisco", "Q62"),
        ("San Fran", "San Francisco", "Q62"),
        ("NYC", "New York City", "Q60"),
        ("Austin, TX", "Austin", "Q16559"),
        ("berkeley", "Berkeley", "Q484678"),
    ],
)
def test_city_normalization(text, city, qid):
    assert canonical_city(text) == (city, qid)


@pytest.mark.parametrize("text", ["Earth", "the internet", "🌍", "California", "Atlantis"])
def test_non_cities_are_not_places(text):
    assert canonical_city(text) is None


def test_sf_normalization_from_recorded_bios():
    claims = recorded_claims("x_parse/bios.json")
    for who in ("Zoe Hart", "Marc Bell", "Ivy Tran", "Rosa Diaz"):
        places = {c.object.identity["wikidata"] for c in by_pred(find(claims, who), "based_in")}
        assert places == {"Q62"}, who
    assert not find(claims, "just vibes")  # nothing to extract


def test_move_keeps_only_the_destination():
    claims = find(recorded_claims("x_parse/bios.json"), "Hugo Lam")
    assert {c.object.identity["wikidata"] for c in by_pred(claims, "based_in")} == {"Q62"}
    (f,) = [c for c in by_pred(claims, "founded") if c.object.name == "Bramble"]
    assert not any(c.object and c.object.name == "Shopwise" for c in claims)  # the buyer is no employer
    assert f.evidence


def test_company_account_gets_no_roles_but_a_place():
    claims = find(recorded_claims("x_parse/bios.json", {"tallyhq_ex": "company"}), "AI controller")
    own = [c for c in claims if c.subject.kind == "company"]
    assert [c.predicate for c in own] == ["based_in"]
    assert own[0].subject.name is None  # known account: no name claim
    # SF is not in this (empty) graph, so the place gets a name claim with evidence
    assert [c.predicate for c in claims if c.subject.kind == "place"] == ["name"]


def test_unknown_account_gets_identity_and_name_with_evidence():
    claims = find(recorded_claims("x_parse/bios.json"), "Marc Bell")
    own = {c.predicate: c for c in claims if c.subject.identity.get("x_handle") == "marc_vc_ex"}
    assert own["x_id"].value == "9000000002" and own["x_handle"].value == "marc_vc_ex"
    assert own["name"].value == "Marc Bell" and own["name"].evidence


def test_github_bio_plain_company_and_past_role():
    claims = find(recorded_claims("x_parse/bios.json"), "Rosa Diaz")
    roles = {(c.object.name, c.valid_to is not None) for c in by_pred(claims, "worked_at")}
    assert roles == {("Vercel", False), ("Heroku", True)}
    assert all(c.subject.identity["github"] == "octo-ex-9" for c in claims if c.predicate == "worked_at")
    assert {_ev(c)["url"] for c in claims} == {"https://github.com/octo-ex-9"}


def test_github_org_handle_is_a_github_identity():
    payload = {
        "user": {
            "login": "octo-ex-7",
            "type": "User",
            "name": "Omar",
            "bio": "co-founder @tinyrocket-ex · rust",
        }
    }
    claims = run(
        "github",
        payload,
        [{"type": "role", "org": "", "org_handle": "tinyrocket-ex", "title": "co-founder", "status": "current",
          "founder": True, "investor": False, "quote": "co-founder @tinyrocket-ex"}],
    )  # fmt: skip
    assert [c.predicate for c in claims if c.object] == ["founded"]  # no title-less duplicate role
    assert by_pred(claims, "founded")[0].subject.identity == {"github": "octo-ex-7"}
    f = by_pred(claims, "founded")[0]
    assert f.object.identity == {"github": "tinyrocket-ex"}


# ── privacy ──


@pytest.mark.parametrize(
    "text",
    [
        "mail me: jo@acme.example",
        "call +1 (415) 555-0134",
        "415-555-0199",
        "+44 20 7946 0958",
        "tel 4155550134",
    ],
)
def test_private_data_detected(text):
    assert has_private_data(text)


@pytest.mark.parametrize(
    "text", ["raised $14M in 2024", "YC W25", "Series A 2019-2021", "10x faster", "@acme_ex"]
)
def test_no_false_private_data(text):
    assert not has_private_data(text)


def test_emails_and_phones_never_emitted():
    payload = {"type": "x_user", "user": {"id": "1", "username": "pat_ex", "name": "Pat",
               "description": "CTO at Gridly. reach me pat@gridly.example or 415-555-0101", "location": ""}}  # fmt: skip
    claims = run(
        "x_spider",
        payload,
        [
            {"type": "role", "org": "Gridly", "org_handle": "", "title": "CTO", "status": "current",
             "founder": False, "investor": False, "quote": "CTO at Gridly"},
            {"type": "role", "org": "Gridly", "org_handle": "", "title": "", "status": "current",
             "founder": False, "investor": False, "quote": "reach me pat@gridly.example"},
            {"type": "milestone", "kind": "other", "title": "Phone 415-555-0101", "org": "", "org_handle": "",
             "first_person": True, "quote": "415-555-0101"},
        ],
    )  # fmt: skip
    assert claims, "the clean role survives"
    for c in claims:
        blob = str(c.model_dump())
        assert "@gridly.example" not in blob and "555-0101" not in blob
    # the recorded bio and post with phone numbers produce nothing private either
    for path in ("x_parse/bios.json", "x_parse/posts.json"):
        for cl in recorded_claims(path).values():
            for c in cl:
                assert not has_private_data(_text_of(c))


def test_people_only_by_handle():
    """A person named without a handle never becomes an entity."""
    payload = {"type": "x_post", "post": {"id": "5", "text": "Welcome Dana Smith to @gridly_ex as CTO!",
               "created_at": "2026-10-01T00:00:00Z", "author_id": "7"}, "author": {"id": "7", "username": "gridly_ex"}}  # fmt: skip
    claims = run(
        "x_spider",
        payload,
        [{"type": "hire", "person_handle": "danasmith", "org": "", "org_handle": "gridly_ex", "title": "CTO",
          "quote": "Welcome Dana Smith to @gridly_ex as CTO!"}],
        account="company",
    )  # fmt: skip
    assert not any(c.subject.kind == "person" for c in claims)


# ── posts: ≥ 10 realistic shapes ──

POSTS_ACCOUNTS = {"tallyhq_ex": "company", "fernhill_ex": "company", "harborvc_ex": "company",
                  "zoe_founder_ex": "person", "marc_vc_ex": "person"}  # fmt: skip


@pytest.fixture(scope="module")
def posts():
    return recorded_claims("x_parse/posts.json", POSTS_ACCOUNTS)


def _milestones(claims) -> set[tuple[str, str]]:
    out = set()
    for c in by_pred(claims, "milestone"):
        who = c.subject.identity.get("x_handle") or c.subject.name
        out.add((who, c.value["kind"]))
        assert c.valid_from == "2026-10-21"  # dated by the post
        assert set(c.value) == {"title", "kind", "url"} and c.value["url"].startswith("https://x.com/")
        assert c.evidence
    return out


def test_post_launching_today(posts):
    assert _milestones(find(posts, "Today we're launching")) == {("tallyhq_ex", "launch")}


def test_post_we_raised(posts):
    claims = find(posts, "We raised a $9M seed")
    assert _milestones(claims) == {("tallyhq_ex", "funding")}
    assert not by_pred(claims, "worked_at")


def test_post_welcome_x_to_y(posts):
    claims = find(posts, "Welcome @ivy_ml_ex to @tallyhq_ex")
    (w,) = by_pred(claims, "worked_at")
    assert w.subject.identity == {"x_handle": "ivy_ml_ex"} and w.subject.kind == "person"
    assert w.object.identity["x_handle"] == "tallyhq_ex" and w.valid_from == "2026-10-21"
    assert _milestones(claims) == {("tallyhq_ex", "hire")}


def test_post_joke_gives_nothing(posts):
    assert find(posts, "office plant") == []


def test_post_hiring_ad_gives_nothing(posts):
    assert find(posts, "We're hiring") == []


def test_post_opinion_thread_gives_nothing(posts):
    assert find(posts, "Unpopular opinion") == []


def test_post_acquisition(posts):
    claims = find(posts, "Fernhill has acquired")
    (a,) = by_pred(claims, "acquired")
    assert a.subject.identity["x_handle"] == "fernhill_ex" and a.object.identity == {
        "x_handle": "sprocket_ex"
    }
    assert a.valid_from == "2026-10-21"
    assert ("fernhill_ex", "acquisition") in _milestones(claims)


def test_post_first_person_joining(posts):
    claims = find(posts, "I'm joining @harborvc_ex")
    (w,) = by_pred(claims, "worked_at")
    assert w.subject.identity["x_handle"] == "marc_vc_ex" and w.object.identity == {"x_handle": "harborvc_ex"}
    assert w.value == {"title": "Partner"} and w.valid_from == "2026-10-21"
    assert _milestones(claims) == {("harborvc_ex", "hire"), ("marc_vc_ex", "hire")}  # company + author


def test_post_product_release(posts):
    assert _milestones(find(posts, "Tally 2.0 is out")) == {("tallyhq_ex", "product")}


def test_post_award(posts):
    assert _milestones(find(posts, "Fintech Startup of the Year")) == {("tallyhq_ex", "award")}


def test_post_with_phone_number_gives_nothing(posts):
    assert find(posts, "Questions about billing") == []


def test_post_investor_announces_portfolio_round(posts):
    claims = find(posts, "Proud to lead the $9M seed")
    assert _milestones(claims) == {("tallyhq_ex", "funding")}  # the startup's milestone, not the fund's


def test_milestone_needs_a_date():
    payload = {"type": "x_post", "post": {"id": "9", "text": "Today we're launching Gridly v1 to everyone!",
               "author_id": "7"}, "author": {"id": "7", "username": "gridly_ex"}}  # fmt: skip
    doc = doc_for(RawRecord.new("x_spider", "p", payload, fetched_at=FETCHED))
    assert doc.meta["date"] == "2026-10-25"  # falls back to the fetch date


# ── articles ──


@pytest.fixture(scope="module")
def articles():
    data = load_recorded("exa_parse/articles.json")
    out = {}
    for item in data["inputs"]:
        doc = _doc(item["source"], item["payload"])
        out[doc.text()] = extract_claims("exa_parse", parsed(doc, data["outputs"][doc.text()]))
    return out


def test_article_funding(articles):
    claims = find(articles, "Tally raises $9M")
    (m,) = by_pred(claims, "milestone")
    assert m.subject.name == "Tally" and m.value["kind"] == "funding" and m.valid_from == "2026-10-21"
    assert m.value["url"] == "https://news.example/2026/10/tally-seed"
    ev = _ev(m)
    assert ev["author"] == "news.example" and ev["quote"] in "\n".join(_doc("exa", load_recorded(
        "exa_parse/articles.json")["inputs"][0]["payload"]).fields.values())  # fmt: skip


def test_article_acquisition(articles):
    claims = find(articles, "Fernhill buys Sprocket")
    (a,) = by_pred(claims, "acquired")
    assert (a.subject.name, a.object.name) == ("Fernhill", "Sprocket") and a.valid_from == "2026-10-19"
    assert {c.subject.name for c in by_pred(claims, "milestone")} == {"Fernhill"}
    assert not any(c.subject.kind == "person" for c in claims)  # articles are about third parties


def test_article_opinion_gives_nothing(articles):
    assert find(articles, "On agents") == []


def test_homepage_results_are_not_parsed():
    payload = {"type": "exa_result", "url": "https://gridly.example/", "title": "Gridly", "homepage": True,
               "published_date": "2026-10-01", "text": "Gridly launched today."}  # fmt: skip
    assert doc_for(RawRecord.new("exa", "u", payload, fetched_at=FETCHED)) is None


def test_x_following_records_are_not_parsed():
    payload = {"type": "x_following", "users": [{"id": "1", "username": "a", "description": "CEO @b"}]}
    assert doc_for(RawRecord.new("x_spider", "f", payload, fetched_at=FETCHED)) is None
