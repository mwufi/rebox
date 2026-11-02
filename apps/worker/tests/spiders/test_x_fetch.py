"""x_spider fetch against an in-memory X API: frontier scoring, priority order, budgets, resume,
stop conditions, and the whole pipeline (resolver + fake writer + Postgres)."""

from __future__ import annotations

import httpx
import pytest

from rebox_worker.connectors.x_spider import XSpider
from rebox_worker.models import CursorUpdate, RawRecord
from rebox_worker.pipeline import load_cursor, run_sync, save_cursor
from rebox_worker.xapi import Spend, XBudget

from .conftest import FakeX, tweet, user

ENV = {"X_BEARER_TOKEN": "fake-token"}


def world() -> FakeX:
    """Two seeds. Seed A (SF investor) follows 5 accounts, seed B follows 4; 3 overlap."""
    a = user("1", "seed_a", name="Seed A", bio="investor", location="San Francisco")
    b = user("2", "seed_b", name="Seed B", bio="founder", location="SF")
    jane = user("10", "jane", name="Jane Doe", bio="founder @acme", location="San Francisco", followers=900)
    acme = user("11", "acme", name="Acme", verified_type="business", url="https://acme.com", followers=5000)
    nolo = user("12", "nolo", name="No Loc", bio="engineer", followers=50)  # no location, 2 seeds
    ny = user("13", "nyc_person", name="New York", bio="founder", location="New York", followers=10)
    zed = user("14", "zedlabs", name="Zed Labs", bio="We build editors", url="https://zed.dev", followers=700)
    kim = user("15", "kim", name="Kim Park", bio="designer", location="Oakland", followers=20)
    deep = [user(str(100 + i), f"deep{i}", name=f"Deep {i}", bio="x") for i in range(3)]
    fx = FakeX(
        [a, b],
        {
            "1": [jane, acme, nolo, ny, zed],
            "2": [jane, acme, nolo, kim],
            "10": deep,  # jane's following (depth-1 expansion)
            "12": deep[:1],
            "15": deep[1:],
        },
    )
    fx.tweets = {
        "11": [tweet(str(900 + i), f"acme post {i}") for i in range(25)],
        "14": [tweet("950", "zed")],
    }
    return fx


def run(fx: FakeX, ctx, cursor=None, *, seeds=None, env=None):
    sp = XSpider(
        transport=fx.transport(), sleep=lambda s: None, seeds=seeds or {"a": ["seed_a"], "b": ["seed_b"]}
    )
    items = list(sp.fetch(ctx(env={**ENV, **(env or {})}), cursor))
    recs = [i for i in items if isinstance(i, RawRecord)]
    cursors = [i.cursor for i in items if isinstance(i, CursorUpdate)]
    return sp, recs, cursors


# ── no DB: seeds + following only ────────────────────────────────────────


def test_disabled_without_token(ctx_factory):
    sp = XSpider(seeds={})
    assert not sp.enabled(ctx_factory(env={}))
    with pytest.raises(RuntimeError, match="X_BEARER_TOKEN"):
        next(iter(sp.fetch(ctx_factory(env={}), None)))
    assert not sp.scheduled  # paid API: never on the automatic schedule


def test_workers_one_per_seed_set():
    sp = XSpider(seeds={"dbooth": ["davidbooth"], "yoni": ["yoni_michael"], "sf-core": []})
    ws = sp.workers()
    assert [(w["name"], w["kind"], w["source"], w["layers"], w["config"]) for w in ws] == [
        ("dbooth", "spider", "x_spider", ["public/x_spider"], {"seeds": ["davidbooth"]}),
        ("yoni", "spider", "x_spider", ["public/x_spider"], {"seeds": ["yoni_michael"]}),
        ("sf-core", "spider", "x_spider", ["public/x_spider"], {"seeds": []}),
    ]


def test_following_records_and_payload_shapes(ctx_factory):
    fx = world()
    sp, recs, cursors = run(fx, ctx_factory, env={"X_SPIDER_PHASES": "following"})
    kinds = [r.payload["type"] for r in recs]
    assert kinds.count("x_following") == 2
    pages = [r for r in recs if r.payload["type"] == "x_following"]
    assert {p.payload["seed"]["username"] for p in pages} == {"seed_a", "seed_b"}
    assert {p.payload["seed_set"] for p in pages} == {"a", "b"}
    assert all(r.source == "x_spider" for r in recs)
    # each page: its profiles come first, then the page record
    first_page = kinds.index("x_following")
    assert kinds[:first_page] == ["x_user"] * first_page
    assert {r.external_id for r in recs if r.payload["type"] == "x_user"} >= {"user:1", "user:2", "user:10"}
    assert cursors[-1]["crawl"]["1"]["done"] and cursors[-1]["crawl"]["2"]["done"]
    assert sp.run_stats["user_reads"] == fx.user_objects == 2 + 5 + 4
    assert sp.run_stats["cost_usd"] == fx.cost == 0.11
    assert sp.run_stats["api_calls"] == len(fx.calls) == 3


def test_budget_split_evenly_between_seeds_and_resume(ctx_factory):
    """A small run budget covers every seed's first page; the next run continues from the token."""
    fx = world()
    env = {"X_MAX_USER_READS_PER_RUN": "6", "X_SPIDER_PHASES": "following"}
    sp, recs, cursors = run(fx, ctx_factory, env=env)
    # 2 seed lookups, then 4 left -> 2 per seed
    assert fx.user_objects == 6
    assert [c for c in fx.calls if "following" in c] == [
        "/2/users/1/following?max_results=2&user.fields=" + fx.calls[1].split("user.fields=")[1],
        "/2/users/2/following?max_results=2&user.fields=" + fx.calls[2].split("user.fields=")[1],
    ]
    assert sp.run_stats["budget_stop"].startswith("no user reads left")
    cur = cursors[-1]
    assert cur["crawl"]["1"] == {**cur["crawl"]["1"], "next": "2", "done": False, "pages": 1, "users": 2}
    # run 2: resumes with pagination_token, seeds are not looked up again
    fx.calls.clear()
    sp2, recs2, cursors2 = run(fx, ctx_factory, cursor=cur, env={"X_SPIDER_PHASES": "following"})
    assert not any("/users/by" in c for c in fx.calls)
    assert all("pagination_token=2" in c for c in fx.calls[:2])
    assert cursors2[-1]["crawl"]["1"]["done"] and cursors2[-1]["crawl"]["2"]["done"]
    assert cursors2[-1]["crawl"]["1"]["users"] == 5
    assert cursors2[-1]["spend"]["user_reads"] == fx.user_objects == 11  # cumulative across runs


def test_total_budget_is_persistent_and_stops_before_overspending(ctx_factory):
    fx = world()
    # $0.05 in total: 2 seeds ($0.02) + 3 users; the next run has nothing left.
    env = {"X_TOTAL_BUDGET_USD": "0.05", "X_SPIDER_PHASES": "following"}
    sp, _, cursors = run(fx, ctx_factory, env=env)
    assert fx.user_objects == 5
    assert cursors[-1]["spend"] == {"user_reads": 5, "post_reads": 0, "api_calls": 3, "usd": 0.05}
    fx.calls.clear()
    sp2, recs2, cursors2 = run(fx, ctx_factory, cursor=cursors[-1], env=env)
    assert fx.calls == [] and recs2 == []
    assert "budget_stop" in sp2.run_stats
    assert cursors2[-1]["spend"]["usd"] == 0.05


@pytest.mark.parametrize(
    ("resp", "marker"),
    [
        (httpx.Response(402, json={"title": "Payment Required", "detail": "credits depleted"}), "HTTP 402"),
        (
            httpx.Response(429, json={"title": "Too Many Requests"}, headers={"x-rate-limit-reset": "1"}),
            "HTTP 429",
        ),
        (
            httpx.Response(
                403, json={"title": "Forbidden", "reason": "usage-capped", "detail": "Usage cap exceeded"}
            ),
            "credit/usage cap",
        ),
        (httpx.Response(401, json={"title": "Unauthorized"}), "HTTP 401"),
    ],
)
def test_stops_cleanly_on_credit_and_rate_errors(ctx_factory, resp, marker):
    fx = world()
    fx.fail["/2/users/2/following"] = resp
    sp, recs, cursors = run(fx, ctx_factory, env={"X_SPIDER_PHASES": "following"})
    assert marker in sp.run_stats["x_stop"]
    assert len([c for c in fx.calls if "/users/2/following" in c]) == 1  # never retried
    assert cursors[-1]["crawl"]["1"]["done"]  # what was fetched before the stop is kept
    assert "2" not in cursors[-1]["crawl"] or not cursors[-1]["crawl"]["2"]["done"]


def test_exhausted_rate_window_stops_before_next_call(ctx_factory):
    fx = world()
    real = fx.handler

    def handler(request):
        r = real(request)
        if request.url.path == "/2/users/by":
            return httpx.Response(200, json=r.json(), headers={"x-rate-limit-remaining": "0"})
        return r

    sp = XSpider(transport=httpx.MockTransport(handler), sleep=lambda s: None, seeds={"a": ["seed_a"]})
    list(sp.fetch(ctx_factory(env=ENV), None))
    assert "window exhausted" in sp.run_stats["x_stop"]
    assert len(fx.calls) == 1


def test_5xx_is_retried(ctx_factory):
    fx = world()
    fx.fail["/2/users/1/following"] = httpx.Response(503)
    sp, _, cursors = run(fx, ctx_factory, seeds={"a": ["seed_a"]}, env={"X_SPIDER_PHASES": "following"})
    assert "x_stop" not in sp.run_stats
    assert cursors[-1]["crawl"]["1"]["done"]


def test_protected_account_is_skipped(ctx_factory):
    fx = world()
    fx.fail["/2/users/1/following"] = httpx.Response(
        200, json={"errors": [{"title": "Authorization Error", "detail": "protected"}]}
    )
    sp, _, cursors = run(fx, ctx_factory, env={"X_SPIDER_PHASES": "following"})
    assert cursors[-1]["crawl"]["1"]["done"] and "protected" in cursors[-1]["crawl"]["1"]["error"]
    assert cursors[-1]["crawl"]["2"]["done"]


def test_limit_keeps_last_checkpoint(ctx_factory):
    """--limit cuts a page short: the cursor keeps the last complete page, spend is still saved."""
    fx = world()
    sp = XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"]})
    items = list(sp.fetch(ctx_factory(env={**ENV, "X_SPIDER_PHASES": "following"}, limit=3), None))
    final = items[-1].cursor
    assert "1" not in final["crawl"]  # the page was cut: it will be read again
    assert final["spend"]["user_reads"] == 1 + 5


# ── with the DB: frontier, posts, expansion, pipeline ────────────────────


def test_frontier_scores_by_distinct_seeds(deps, owner):
    fx = world()
    sp = XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"], "b": ["seed_b"]})
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following"}
    run_sync(sp, deps)
    rows = owner.execute(
        "select key, kind, label, score, depth, discovered_by, status from frontier where source='x_spider' "
        "order by score desc, key"
    ).fetchall()
    by = {r["key"]: r for r in rows}
    assert by["10"]["score"] == 2 and by["10"]["discovered_by"] == ["seed_a", "seed_b"]
    assert by["11"]["kind"] == "company" and by["11"]["score"] == 2
    assert by["13"]["score"] == 1 and by["13"]["discovered_by"] == ["seed_a"]
    assert by["1"]["depth"] == 0 and by["1"]["status"] == "fetched"  # seeds are marked crawled
    assert by["1"]["discovered_by"] == ["seed-set:a"]
    assert by["10"]["depth"] == 1 and by["10"]["status"] == "new"
    # re-running the same pages does not inflate scores
    save_cursor(deps.db, "x_spider", {})
    run_sync(
        XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"], "b": ["seed_b"]}),
        deps,
    )
    assert (
        owner.execute("select score from frontier where source='x_spider' and key='10'").fetchone()["score"]
        == 2
    )


def test_pipeline_writes_profiles_and_follows(deps, owner):
    fx = world()
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following"}
    stats = run_sync(
        XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"], "b": ["seed_b"]}),
        deps,
    )
    assert stats.rejected == 0 and stats.extract_errors == 0
    kinds = {r["kind"]: r["n"] for r in owner.execute("select kind, count(*) n from entity group by kind")}
    assert kinds == {"person": 6, "company": 2}  # seeds, jane, nolo, nyc_person, kim | acme, zedlabs
    follows = owner.execute(
        """select s.value_text sub, o.value_text obj from claim f
             join claim s on s.subject = f.subject and s.predicate = 'x_handle'
             join claim o on o.subject = f.object_id and o.predicate = 'x_handle'
            where f.predicate = 'follows' order by 1, 2"""
    ).fetchall()
    assert [(r["sub"], r["obj"]) for r in follows] == [
        ("seed_a", "acme"), ("seed_a", "jane"), ("seed_a", "nolo"),
        ("seed_a", "nyc_person"), ("seed_a", "zedlabs"),
        ("seed_b", "acme"), ("seed_b", "jane"), ("seed_b", "kim"), ("seed_b", "nolo"),
    ]  # fmt: skip
    acme = owner.execute("select subject from claim where predicate='x_id' and value_text='11'").fetchone()[
        "subject"
    ]
    assert acme.startswith("rbx:company:")
    facts = {
        r["predicate"]: r["value"]
        for r in owner.execute(
            "select predicate, value from claim where subject=%s and object_id is null", (acme,)
        )
    }
    assert facts["domain"] == "acme.com" and facts["name"] == "Acme" and facts["x_followers"] == 5000
    assert owner.execute("select count(distinct layer_id) n from claim").fetchone()["n"] == 1


def test_matches_existing_entity_by_x_handle(deps, owner, writer):
    """An account already in the graph (from Wikidata/X enrichment) is matched, not duplicated."""
    import json
    import urllib.request

    body = {
        "layer": "public/x",
        "claims": [
            {"subject": {"mint": {"id": "rbx:person:existingjane", "kind": "person"}}, "predicate": "name",
             "value": "Jane D.", "source_ref": "t:1"},
            {"subject": {"id": "rbx:person:existingjane"}, "predicate": "x_handle", "value": "jane",
             "source_ref": "t:2"},
        ],
    }  # fmt: skip
    req = urllib.request.Request(
        writer.url + "/internal/claims",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {writer.token}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req).read()
    fx = world()
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following"}
    run_sync(XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"]}), deps)
    rows = owner.execute(
        "select distinct subject from claim where predicate='x_id' and value_text='10'"
    ).fetchall()
    assert [r["subject"] for r in rows] == ["rbx:person:existingjane"]
    assert (
        owner.execute(
            "select count(*) n from claim where predicate='follows' and object_id='rbx:person:existingjane'"
        ).fetchone()["n"]
        == 1
    )


def test_posts_after_following_companies_only_within_budget(deps, owner):
    fx = world()
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following,posts", "X_MAX_POST_READS_PER_RUN": "25"}
    sp = XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"], "b": ["seed_b"]})
    run_sync(sp, deps)
    post_calls = [c for c in fx.calls if "/tweets" in c]
    # acme (score 2) first with 20, then zed (score 1) with the 5 left; people never
    assert [c.split("?")[0] for c in post_calls] == ["/2/users/11/tweets", "/2/users/14/tweets"]
    assert "max_results=20" in post_calls[0] and "max_results=5" in post_calls[1]
    assert fx.post_objects == 21 and sp.run_stats["post_reads"] == 21
    raw = owner.execute(
        "select count(*) n from raw_record where source='x_spider' and external_id like 'post:%'"
    ).fetchone()
    assert raw["n"] == 21
    assert (
        owner.execute("select count(*) n from claim where source_ref like 'x_spider:post%'").fetchone()["n"]
        == 0
    )


def test_posts_and_expansion_wait_for_seed_coverage(deps):
    fx = world()
    deps.ctx.env = {**ENV, "X_MAX_USER_READS_PER_RUN": "6"}
    sp = XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"], "b": ["seed_b"]})
    run_sync(sp, deps)
    assert not any("/tweets" in c for c in fx.calls)
    assert {c.split("?")[0] for c in fx.calls} == {
        "/2/users/by",
        "/2/users/1/following",
        "/2/users/2/following",
    }


def test_expansion_order_and_budget(deps, owner):
    """Depth-1 SF people by score (then followers): jane (2 seeds, SF), nolo (2 seeds, no location),
    kim (Oakland, 1 seed); not nyc_person (New York) and not companies."""
    fx = world()
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following,expansion"}
    sp = XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"], "b": ["seed_b"]})
    run_sync(sp, deps)
    order = [c.split("?")[0] for c in fx.calls if "following" in c][2:]
    assert order == ["/2/users/10/following", "/2/users/12/following", "/2/users/15/following"]
    rows = {
        r["key"]: r
        for r in owner.execute("select key, depth, status, score from frontier where source='x_spider'")
    }
    assert rows["10"]["status"] == "fetched" and rows["13"]["status"] == "new"
    assert rows["100"]["depth"] == 2 and rows["100"]["score"] == 2  # jane + nolo
    assert rows["102"]["score"] == 2  # jane + kim
    assert sp.run_stats["expansion"] == "no eligible accounts left"


def test_expansion_stops_at_budget_and_resumes(deps):
    fx = world()
    # seeds 2 + following 9 = 11; 2 more users -> jane's first page of 2, then stop
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following,expansion", "X_MAX_USER_READS_PER_RUN": "13"}

    def mk():
        return XSpider(
            transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"], "b": ["seed_b"]}
        )

    sp = mk()
    run_sync(sp, deps)
    cur = load_cursor(deps.db, sp)
    assert cur["crawl"]["10"]["done"] is False and cur["crawl"]["10"]["expansion"] is True
    assert "budget_stop" in sp.run_stats
    fx.calls.clear()
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following,expansion"}
    run_sync(mk(), deps)
    assert fx.calls[0].startswith("/2/users/10/following") and "pagination_token=2" in fx.calls[0]


def test_spend_persisted_even_if_run_crashes(deps):
    fx = world()
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following"}
    sp = XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={"a": ["seed_a"]})
    from dataclasses import replace

    gen = sp.fetch(replace(deps.ctx, db=deps.db), None)
    next(gen)  # seed lookup done and charged
    gen.close()
    assert load_cursor(deps.db, sp)["spend"]["user_reads"] == 1


def test_budget_math():
    b = XBudget(total_usd=1.0, run_user_cap=50, run_post_cap=10, total=Spend(usd=0.95))
    assert b.users_allowed(1000) == 5
    assert b.posts_allowed(20) == 10
    b.charge(users=5)
    assert b.users_allowed(1) == 0 and b.posts_allowed(5) == 0
    assert b.run.api_calls == 1 and round(b.total.usd, 4) == 1.0
