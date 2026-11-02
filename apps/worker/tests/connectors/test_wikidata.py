"""Wikidata: grouping per organization, class filter, extraction, fetch."""

from __future__ import annotations

import httpx
import pytest

from rebox_worker.connectors.wikidata import (
    ENTITY,
    Wikidata,
    _canonical,
    _cities,
    company_domain,
    discover,
    founded_year,
    headcount,
    keep_org,
)
from rebox_worker.models import CursorUpdate, RawRecord

from .conftest import ctx, facts, record, records


def _ext(qid: str):
    return Wikidata().extract(record("wikidata", qid))


def test_stripe_company_people_and_edges():
    claims = _ext("Q7624104")
    f = facts(claims)
    assert {
        ("company", "Stripe", "name", "Stripe"),
        ("company", "Stripe", "wikidata", "Q7624104"),
        ("company", "Stripe", "website", "https://stripe.com/"),
        ("company", "Stripe", "domain", "stripe.com"),
        ("company", "Stripe", "x_handle", "stripe"),
        ("company", "Stripe", "github", "stripe"),
        ("company", "Stripe", "founded_year", 2010),
        ("company", "Stripe", "headcount", 8000),  # the latest P1128 (2022), not 2020's 2500
        ("company", "Stripe", "based_in", "San Francisco"),
        ("place", "San Francisco", "wikidata", "Q62"),
        ("person", "Patrick Collison", "founded", "Stripe"),
        ("person", "John Collison", "founded", "Stripe"),
        ("person", "Patrick Collison", "ceo_of", "Stripe"),
        ("person", "Patrick Collison", "x_handle", "patrickc"),
    } <= f
    company = next(c.subject for c in claims if c.predicate == "name" and c.subject.kind == "company")
    assert company.identity == {
        "wikidata": "Q7624104",
        "domain": ["stripe.com"],
        "github": ["stripe"],
        "x_handle": ["stripe"],
    }
    # People carry the company as context, so other sources' records of them match by name.
    patrick = next(
        c.subject for c in claims if c.predicate == "founded" and c.subject.name == "Patrick Collison"
    )
    assert patrick.identity["wikidata"] == "Q7146257"
    assert patrick.context and patrick.context[0].identity["wikidata"] == "Q7624104"


def test_ceo_terms_have_dates_and_distinct_source_refs():
    claims = _ext("Q21708200")  # OpenAI: Sam Altman CEO twice
    terms = sorted(
        (c.valid_from, c.valid_to, c.source_ref)
        for c in claims
        if c.predicate == "ceo_of" and c.subject.name == "Sam Altman"
    )
    assert [(a, b) for a, b, _ in terms] == [("2019-03-01", "2023-11-17"), ("2023-11-22", None)]
    assert len({r for _, _, r in terms}) == 2


def test_one_record_groups_all_bindings_of_an_organization():
    rec = record("wikidata", "Q21708200")
    props = {b["prop"] for b in rec.payload["bindings"]}
    assert {"label", "description", "P31", "P856", "P571", "P112", "P169", "P1128"} <= props
    assert len(rec.payload["people"]) >= 10  # every founder/CEO with their own bindings
    assert rec.payload["bindings"] == _canonical(reversed(rec.payload["bindings"]))  # canonical order
    assert all(r.external_id == r.payload["qid"] for r in records("wikidata"))


def test_shared_domain_and_handle_are_not_identity():
    """GitHub (Q364) and GitHub, Inc. (Q28771536) share github.com and @github: they must not
    be matched onto each other by those keys, but the facts are still recorded."""
    for qid in ("Q364", "Q28771536"):
        claims = _ext(qid)
        company = next(c.subject for c in claims if c.predicate == "name" and c.subject.kind == "company")
        assert "domain" not in company.identity and "x_handle" not in company.identity
        assert ("company", company.name, "website", "https://github.com") in facts(claims)
        assert any(c.predicate == "x_handle" for c in claims if c.subject.kind == "company")
    mozilla = next(c.subject for c in _ext("Q9661") if c.predicate == "name")
    assert "domain" not in mozilla.identity and "x_handle" not in mozilla.identity
    assert mozilla.identity["github"] == ["mozilla"]  # not shared, so still an identity


def test_founders_that_are_not_humans_are_skipped():
    for rec in records("wikidata"):
        for c in Wikidata().extract(rec):
            if c.predicate in ("founded", "ceo_of"):
                assert c.subject.kind == "person" and c.subject.identity.get("wikidata")


def test_extract_is_pure_and_refs_unique():
    for rec in records("wikidata"):
        a = Wikidata().extract(rec)
        assert [c.model_dump() for c in a] == [c.model_dump() for c in Wikidata().extract(rec)]
        keys = [(c.source_ref, c.predicate, c.subject.identity.get("wikidata")) for c in a]
        assert len(keys) == len(set(keys)), rec.external_id
        assert all(c.source_ref.startswith("wikidata:Q") for c in a)
        assert sum(1 for c in a if c.predicate == "name" and c.subject.kind == "company") == 1


def test_fixture_size():
    assert 20 <= len(records("wikidata")) <= 40


@pytest.mark.parametrize(
    ("site", "name", "expected"),
    [
        ("https://stripe.com/", "Stripe", "stripe.com"),
        ("https://www.mozilla.org/en-US/", "Mozilla", "mozilla.org"),
        ("https://github.com", "GitHub", "github.com"),  # a platform's own root
        ("https://books.google.com/books/about", "Some Imprint", None),
        ("https://www.acme.com/products/x", "Acme", "acme.com"),
        ("https://github.com/acme", "Acme", None),
    ],
)
def test_company_domain(site, name, expected):
    assert company_domain(site, name) == expected


def test_value_helpers():
    assert founded_year(["2010-01-01T00:00:00Z", "2009-06-01T00:00:00Z"]) == 2009
    assert founded_year(["-0500-01-01T00:00:00Z"]) is None
    rows = [
        {
            "prop": "P1128",
            "value": "100",
            "rank": "http://wikiba.se/ontology#NormalRank",
            "time": "2024-01-01",
        },
        {
            "prop": "P1128",
            "value": "50",
            "rank": "http://wikiba.se/ontology#PreferredRank",
            "time": "2020-01-01",
        },
        {
            "prop": "P1128",
            "value": "999",
            "rank": "http://wikiba.se/ontology#DeprecatedRank",
            "time": "2025-01-01",
        },
    ]
    assert headcount(rows) == 50  # preferred rank beats a newer normal one; deprecated ignored
    assert keep_org({"Q4830453"}) and keep_org({"Q4830453", "Q35127"})
    assert not keep_org({"Q5"}) and not keep_org(set()) and not keep_org({"Q11032"})
    assert _cities(None) == ("Q62",) and _cities("q62, Q49255") == ("Q62", "Q49255")
    with pytest.raises(ValueError):
        _cities("San Francisco")


def test_discover_groups_rows():
    sf = {"city": ENTITY + "Q62", "cityLabel": "San Francisco"}
    rows = [
        {"item": ENTITY + "Q1", "cls": ENTITY + "Q4830453", "website": "https://a.example", **sf},
        {"item": ENTITY + "Q1", "cls": ENTITY + "Q35127", "inception": "2019-01-01T00:00:00Z", **sf},
        {"item": ENTITY + "Q2", "cls": ENTITY + "Q5", **sf},
    ]
    found = discover(rows)
    assert found["Q1"]["classes"] == {"Q4830453", "Q35127"} and found["Q1"]["inception_year"] == 2019
    assert found["Q1"]["cities"] == {"Q62": "San Francisco"}
    assert keep_org(found["Q1"]["classes"]) and not keep_org(found["Q2"]["classes"])


# ── fetch (mock SPARQL endpoint answering from the recorded records) ──────


def _sparql_from_fixtures():
    recs = {r.external_id: r.payload for r in records("wikidata")}
    calls: list[str] = []

    def b(**kw: str) -> dict:
        return {k: {"type": "literal", "value": v} for k, v in kw.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["query"]
        calls.append(q)
        assert request.headers["Accept"] == "application/sparql-results+json"
        rows: list[dict] = []
        if "?cityLabel" in q:
            for qid in recs:
                rows.append(
                    b(
                        item=ENTITY + qid,
                        city=ENTITY + "Q62",
                        cityLabel="San Francisco",
                        cls=ENTITY + "Q4830453",
                    )
                )
            rows.append(
                b(
                    item=ENTITY + "Q99999999",
                    city=ENTITY + "Q62",
                    cityLabel="San Francisco",
                    cls=ENTITY + "Q5",
                )
            )
        elif "?x ?gh" in q:
            pass
        else:
            wanted = [w.removeprefix("wd:") for w in q.split("VALUES ?item {")[1].split("}")[0].split()]
            for qid in wanted:
                src = (
                    recs[qid]["bindings"]
                    if qid in recs
                    else next((p[qid] for r in recs.values() for k, p in [(0, r["people"])] if qid in p), [])
                )
                for row in src:
                    cell = {k: {"type": "literal", "value": v} for k, v in row.items() if k != "lang"}
                    if "lang" in row:
                        cell["value"]["xml:lang"] = row["lang"]
                    rows.append({"item": {"type": "uri", "value": ENTITY + qid}, **cell})
        return httpx.Response(200, json={"head": {}, "results": {"bindings": rows}})

    return handler, calls, recs


def test_fetch_groups_one_record_per_org_and_reproduces_payloads():
    handler, calls, recs = _sparql_from_fixtures()
    conn = Wikidata(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    items = list(conn.fetch(ctx(), None))
    out = {i.external_id: i for i in items if isinstance(i, RawRecord)}
    assert set(out) == set(recs)  # the human (Q5) was filtered out
    for qid, rec in out.items():
        assert rec.payload["bindings"] == recs[qid]["bindings"]
        assert rec.payload["people"] == recs[qid]["people"]
    assert isinstance(items[-1], CursorUpdate) and items[-1].cursor["of"] == len(recs)
    assert sum(1 for c in calls if "p:P1128" in c) == -(-len(recs) // 50)  # batched detail queries


def test_fetch_limit():
    handler, _, _ = _sparql_from_fixtures()
    conn = Wikidata(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert len([i for i in conn.fetch(ctx(limit=4), None) if isinstance(i, RawRecord)]) == 4


def test_fetch_backs_off_on_429():
    slept: list[float] = []
    seq = [httpx.Response(429, headers={"Retry-After": "5"})]

    def handler(request):
        return seq.pop(0) if seq else httpx.Response(200, json={"results": {"bindings": []}})

    conn = Wikidata(transport=httpx.MockTransport(handler), sleep=slept.append, min_interval_s=2.0)
    assert list(conn.fetch(ctx(), None)) == []
    assert slept[:3] == [2.0, 5.0, 2.0]
