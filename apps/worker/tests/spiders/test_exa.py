"""Exa discovery: request shape, budget/round-robin cursor, homepage vs article extraction, frontier."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from rebox_worker.connectors.exa import Exa, homepage_name, result_payload
from rebox_worker.frontier import ExaQuery
from rebox_worker.models import CursorUpdate, RawRecord
from rebox_worker.pipeline import run_sync

from .conftest import fake_exa

ENV = {"EXA_API_KEY": "fake-exa"}
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)

RESULTS = {
    "launches": [
        {
            "url": "https://www.acme.ai/",
            "title": "Acme AI | Agents for accountants",
            "publishedDate": "2026-10-20",
        },
        {
            "url": "https://techcrunch.com/2026/10/21/acme-raises-seed/",
            "title": "Acme raises $5M seed to build agents for accountants",
            "publishedDate": "2026-10-21T10:00:00.000Z",
            "author": "Some Writer",
            "text": "SAN FRANCISCO — Acme, a startup ...",
            "highlights": ["Acme raised $5M led by Example Ventures."],
        },
        {"url": "https://blog.zed.dev/launch", "title": "We're launching Zed 2"},
    ],
    "named": [{"url": "https://ditto.ai", "title": "Ditto — The first AI social agent"}],
}


def queries() -> list[ExaQuery]:
    return [
        ExaQuery(q="Ditto", include_domains=["ditto.ai"], num=3),
        ExaQuery(q="SF launches", days=30),
        ExaQuery(q="SF seed rounds", category="news", days=7),
    ]


def results_for(body):
    return RESULTS["named"] if body.get("includeDomains") else RESULTS["launches"]


def run(ctx, cursor=None, env=None, qs=None):
    bodies: list[dict] = []
    exa = Exa(
        transport=fake_exa(results_for, bodies),
        sleep=lambda s: None,
        queries=qs or queries(),
        now=lambda: NOW,
    )
    items = list(exa.fetch(ctx(env={**ENV, **(env or {})}), cursor))
    return (
        exa,
        bodies,
        [i for i in items if isinstance(i, RawRecord)],
        [i.cursor for i in items if isinstance(i, CursorUpdate)],
    )


def test_disabled_without_key(ctx_factory):
    ex = Exa(queries=[])
    assert not ex.enabled(ctx_factory(env={}))
    with pytest.raises(RuntimeError, match="EXA_API_KEY"):
        next(iter(ex.fetch(ctx_factory(env={}), None)))


def test_request_shape(ctx_factory):
    _, bodies, _, _ = run(ctx_factory)
    named, launches, news = bodies
    assert named["includeDomains"] == ["ditto.ai"] and named["numResults"] == 3
    assert "startPublishedDate" not in named  # named startups: no date filter
    assert launches["numResults"] == 25 and launches["startPublishedDate"] == "2026-10-03T00:00:00.000Z"
    assert news["category"] == "news" and news["startPublishedDate"] == "2026-10-26T00:00:00.000Z"
    assert launches["contents"]["text"] == {"maxCharacters": 3000}
    assert "highlights" in launches["contents"] and "summary" in launches["contents"]


def test_records_dedupe_and_stats(ctx_factory):
    exa, _, recs, cursors = run(ctx_factory)
    # launches returned by two queries -> one record each
    assert sorted(r.external_id for r in recs) == [
        "url:https://acme.ai",
        "url:https://blog.zed.dev/launch",
        "url:https://ditto.ai",
        "url:https://techcrunch.com/2026/10/21/acme-raises-seed",
    ]
    tc = next(r for r in recs if "techcrunch" in r.url)
    assert tc.payload["type"] == "exa_result" and tc.payload["homepage"] is False
    assert tc.payload["published_date"] == "2026-10-21T10:00:00.000Z"
    assert tc.payload["highlights"] == ["Acme raised $5M led by Example Ventures."]
    assert exa.run_stats == {
        "api_calls": 3,
        "cost_usd": 0.036,
        "results": 4,
        "homepages": 2,
        "queries": 3,
    }
    assert cursors[-1]["next_query"] == 0


def test_request_budget_and_round_robin(ctx_factory):
    _, bodies, _, cursors = run(ctx_factory, env={"EXA_MAX_REQUESTS_PER_RUN": "2"})
    assert len(bodies) == 2 and cursors[-1]["next_query"] == 2
    _, bodies2, _, cursors2 = run(ctx_factory, cursor=cursors[-1], env={"EXA_MAX_REQUESTS_PER_RUN": "2"})
    assert [b["query"] for b in bodies2] == ["SF seed rounds", "Ditto"]
    assert cursors2[-1]["next_query"] == 1


def test_error_is_not_retried_on_4xx(ctx_factory):
    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    exa = Exa(transport=httpx.MockTransport(handler), sleep=lambda s: None, queries=queries())
    with pytest.raises(Exception, match="HTTP 401"):
        list(exa.fetch(ctx_factory(env=ENV), None))


@pytest.mark.parametrize(
    ("url", "title", "homepage", "name"),
    [
        ("https://www.acme.ai/", "Acme AI | Agents for accountants", True, "Acme AI"),
        ("https://ditto.ai", "Ditto — The first AI social agent", True, "Ditto"),
        ("https://knowndating.com/", "Known: dating, by voice", True, "Known"),
        ("https://acme.ai/en", "Home - Acme", True, "Acme"),
        ("https://getnao.io/", "Nao Labs - the data IDE", True, "Nao Labs"),
        ("https://acme.ai/blog/launch", "Launch", False, None),
        ("https://techcrunch.com/", "TechCrunch", False, None),
        ("https://github.com/", "GitHub", False, None),
        ("https://www.ycombinator.com/companies/acme", "Acme | Y Combinator", False, None),
    ],
)
def test_homepage_detection_and_name(url, title, homepage, name):
    p = result_payload({"url": url, "title": title}, ExaQuery(q="x"))
    assert p["homepage"] is homepage
    if homepage:
        assert homepage_name(p) == name


def test_extract_homepage_only():
    ex = Exa(queries=[])
    home = RawRecord.new(
        "exa",
        "url:https://acme.ai",
        result_payload({"url": "https://www.acme.ai/", "title": "Acme AI | Agents"}, ExaQuery(q="x")),
    )
    art = RawRecord.new("exa", "url:x", result_payload(RESULTS["launches"][1], ExaQuery(q="x")))
    cs = ex.extract(home)
    assert {(c.predicate, c.value) for c in cs} == {
        ("domain", "acme.ai"),
        ("name", "Acme AI"),
        ("website", "https://acme.ai"),
    }
    assert cs[0].subject.kind == "company" and cs[0].subject.identity == {"domain": "acme.ai"}
    assert {c.source_ref for c in cs} == {
        "exa:domain:acme.ai:domain",
        "exa:domain:acme.ai:name",
        "exa:domain:acme.ai:website",
    }
    assert ex.extract(art) == []


def test_pipeline_frontier_and_claims(deps, owner):
    bodies: list[dict] = []
    deps.ctx.env = ENV
    exa = Exa(
        transport=fake_exa(results_for, bodies), sleep=lambda s: None, queries=queries(), now=lambda: NOW
    )
    stats = run_sync(exa, deps)
    assert stats.rejected == 0
    rows = {
        r["key"]: r
        for r in owner.execute(
            "select key, kind, label, score, discovered_by from frontier where source='exa'"
        )
    }
    assert set(rows) == {"acme.ai", "ditto.ai"}
    assert rows["acme.ai"]["score"] == 2 and rows["acme.ai"]["label"] == "Acme AI"
    assert rows["ditto.ai"]["discovered_by"] == ["exa:Ditto"]
    names = {
        r["value_text"]
        for r in owner.execute(
            "select value_text from claim where predicate='name' and layer_id='public/exa'"
        )
    }
    assert names == {"Acme AI", "Ditto"}
    assert owner.execute("select count(*) n from raw_record where source='exa'").fetchone()["n"] == 4
