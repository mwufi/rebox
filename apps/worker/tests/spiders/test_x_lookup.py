"""x_spider ``lookup`` phase (S15): frontier rows queued by handle (``@<handle>``) are looked up
with ``GET /2/users/by`` against an in-memory X API (no live calls)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from rebox_worker import frontier
from rebox_worker.connectors.x_spider import PHASES, XSpider, lookup_errors
from rebox_worker.models import CursorUpdate, RawRecord
from rebox_worker.pipeline import run_sync

from .conftest import FakeX, user

ENV = {"X_BEARER_TOKEN": "fake-token"}
BY = ["coverage:list"]


class LookupX(FakeX):
    """``/2/users/by`` answers like X: found users in ``data``, one error per missing or
    suspended username in ``errors``."""

    def __init__(self, users: list[dict[str, Any]], *, suspended: tuple[str, ...] = (), **kw: Any) -> None:
        super().__init__(users, kw.get("following", {}))
        self.suspended = set(suspended)
        self.lookups: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/2/users/by":
            return super().handler(request)
        assert request.headers["Authorization"] == "Bearer fake-token"
        self.calls.append(f"{request.url.path}?{request.url.query.decode()}")
        if request.url.path in self.fail:
            return self.fail.pop(request.url.path)
        names = request.url.params["usernames"].split(",")
        assert len(names) <= 100 and "public_metrics" in request.url.params["user.fields"]
        self.lookups.append(names)
        found = [self.users[h] for h in names if h in self.users and h not in self.suspended]
        errors = [
            {
                "value": h,
                "detail": f"User has been suspended: [{h}]."
                if h in self.suspended
                else f"Could not find user with usernames: [{h}].",
                "title": "Forbidden" if h in self.suspended else "Not Found Error",
                "resource_type": "user",
                "parameter": "usernames",
                "resource_id": h,
                "type": "https://api.twitter.com/2/problems/resource-not-found",
            }
            for h in names
            if h not in self.users or h in self.suspended
        ]
        self.user_objects += len(found)
        body: dict[str, Any] = {}
        if found:
            body["data"] = found
        if errors:
            body["errors"] = errors
        return httpx.Response(200, json=body)


def _queue(db, *handles: str, score_first: str | None = None) -> None:
    with db.view() as conn:
        frontier.upsert(
            conn,
            "x_spider",
            [
                frontier.Item(
                    key=f"@{h}",
                    kind="person",
                    label=h,
                    discovered_by=[*BY, "coverage:vip"] if h == score_first else BY,
                    payload={"via": "coverage", "username": h},
                )
                for h in handles
            ],
        )


def _rows(owner) -> dict[str, dict[str, Any]]:
    return {r["key"]: r for r in owner.execute("select * from frontier where source = 'x_spider'")}


def _run(fx: FakeX, ctx, db, *, env: dict[str, str] | None = None, cursor=None, seeds=None, limit=None):
    sp = XSpider(transport=fx.transport(), sleep=lambda s: None, seeds=seeds if seeds is not None else {})
    items = list(sp.fetch(ctx(env={**ENV, **(env or {})}, db=db, limit=limit), cursor))
    recs = [i for i in items if isinstance(i, RawRecord)]
    cursors = [i.cursor for i in items if isinstance(i, CursorUpdate)]
    return sp, recs, cursors


def _world() -> LookupX:
    return LookupX(
        [
            user("101", "Alice", name="Alice A", bio="founder", location="SF", followers=10),
            user("102", "acmehq", name="Acme", verified_type="business", url="https://acme.com"),
            user("103", "banned", name="Banned"),
            user("1", "seed_a", name="Seed A"),
        ],
        suspended=("banned",),
    )


def test_lookup_is_the_first_default_phase():
    assert PHASES[0] == "lookup" and set(PHASES) == {"lookup", "following", "posts", "expansion"}


def test_lookup_found_missing_suspended_invalid(db, owner, ctx_factory):
    fx = _world()
    _queue(db, "alice", "acmehq", "ghost", "banned", "not-a-handle!", score_first="acmehq")
    sp, recs, cursors = _run(fx, ctx_factory, db, env={"X_SPIDER_PHASES": "lookup"})

    # one batched call; the invalid handle never reaches X; highest score first
    assert fx.lookups == [["acmehq", "alice", "banned", "ghost"]]
    assert [r.external_id for r in recs] == ["user:102", "user:101"]
    assert all(r.payload["type"] == "x_user" and r.source == "x_spider" for r in recs)

    rows = _rows(owner)
    assert rows["@alice"]["status"] == "fetched" and rows["@alice"]["payload"]["x_id"] == "101"
    assert rows["@alice"]["fetched_at"] is not None
    assert rows["@ghost"]["status"] == "failed"
    assert rows["@ghost"]["payload"]["lookup_error"].startswith("not found: Could not find user")
    assert rows["@banned"]["status"] == "failed"
    assert rows["@banned"]["payload"]["lookup_error"].startswith("suspended: User has been suspended")
    assert rows["@not-a-handle!"]["status"] == "failed"
    assert rows["@not-a-handle!"]["payload"]["lookup_error"].startswith("invalid")
    assert rows["@ghost"]["payload"]["via"] == "coverage"  # the queued payload is kept

    # each found account is a frontier row keyed by user id: depth 0, discovered_by carried over
    assert rows["101"]["depth"] == 0 and rows["101"]["discovered_by"] == BY and rows["101"]["status"] == "new"
    assert rows["101"]["kind"] == "person" and rows["101"]["label"] == "Alice"
    assert rows["102"]["kind"] == "company" and sorted(rows["102"]["discovered_by"]) == [
        "coverage:list",
        "coverage:vip",
    ]
    assert rows["102"]["score"] == 2

    # charged like any user read (2 user objects came back)
    assert sp.run_stats["user_reads"] == fx.user_objects == 2 and sp.run_stats["cost_usd"] == 0.02
    assert sp.run_stats["lookup"] == {"found": 2, "missing": 1, "suspended": 1, "invalid": 1}
    assert cursors[-1]["spend"]["user_reads"] == 2
    # lookup-only: the seeds were not looked up
    assert len(fx.calls) == 1


def test_lookup_runs_first_and_is_idempotent(db, owner, ctx_factory):
    fx = _world()
    _queue(db, "alice", "ghost")
    sp, recs, cursors = _run(
        fx, ctx_factory, db, seeds={"a": ["seed_a"]}, env={"X_SPIDER_PHASES": "lookup,following"}
    )
    assert fx.calls[0].startswith("/2/users/by?usernames=alice%2Cghost")  # before the seed lookup
    assert fx.calls[1].startswith("/2/users/by?usernames=seed_a")
    assert {r.external_id for r in recs} >= {"user:101", "user:1"}

    # next run: nothing is looked up again
    fx.calls.clear()
    fx.lookups.clear()
    sp2, recs2, _ = _run(fx, ctx_factory, db, cursor=cursors[-1], seeds={"a": ["seed_a"]})
    assert fx.lookups == [] and not any(r.external_id == "user:101" for r in recs2)
    assert "lookup" not in sp2.run_stats


def test_lookup_batches_of_100(db, owner, ctx_factory):
    handles = [f"user{i:03d}" for i in range(150)]
    fx = LookupX([user(str(1000 + i), h) for i, h in enumerate(handles)])
    _queue(db, *handles)
    sp, recs, _ = _run(fx, ctx_factory, db, env={"X_SPIDER_PHASES": "lookup"})
    assert [len(b) for b in fx.lookups] == [100, 50]
    assert len(recs) == 150 and sp.run_stats["lookup"] == {"found": 150}
    assert (
        owner.execute(
            "select count(*) n from frontier where key like '@%%' and status = 'fetched'"
        ).fetchone()["n"]
        == 150
    )


def test_lookup_stops_cleanly_at_the_budget(db, owner, ctx_factory):
    fx = _world()
    _queue(db, "alice", "acmehq", "ghost", score_first="acmehq")
    # $0.02 in total = 2 user reads: only acmehq and alice fit; ghost waits for the next run
    env = {"X_SPIDER_PHASES": "lookup", "X_TOTAL_BUDGET_USD": "0.02"}
    sp, recs, cursors = _run(fx, ctx_factory, db, env=env)
    assert fx.lookups == [["acmehq", "alice"]]
    assert "budget_stop" in sp.run_stats and len(recs) == 2
    rows = _rows(owner)
    assert rows["@ghost"]["status"] == "new" and rows["@alice"]["status"] == "fetched"
    assert cursors[-1]["spend"]["usd"] == 0.02
    # the total is spent: the next run calls nothing
    fx.lookups.clear()
    sp2, recs2, _ = _run(fx, ctx_factory, db, env=env, cursor=cursors[-1])
    assert fx.lookups == [] and recs2 == [] and sp2.run_stats["budget_stop"].startswith("no user reads left")
    # with budget again, the rest is looked up
    sp3, recs3, _ = _run(fx, ctx_factory, db, env={"X_SPIDER_PHASES": "lookup"}, cursor=cursors[-1])
    assert fx.lookups == [["ghost"]] and _rows(owner)["@ghost"]["status"] == "failed"


def test_lookup_per_run_cap(db, owner, ctx_factory):
    fx = _world()
    _queue(db, "alice", "acmehq", "ghost")
    sp, recs, _ = _run(
        fx, ctx_factory, db, env={"X_SPIDER_PHASES": "lookup", "X_MAX_USER_READS_PER_RUN": "1"}
    )
    assert fx.lookups == [["acmehq"]] and len(recs) == 1 and "budget_stop" in sp.run_stats


def test_lookup_x_stop_and_refused_batch(db, owner, ctx_factory):
    fx = _world()
    _queue(db, "alice")
    fx.fail["/2/users/by"] = httpx.Response(429, json={"title": "Too Many Requests"})
    sp, recs, _ = _run(fx, ctx_factory, db, env={"X_SPIDER_PHASES": "lookup"})
    assert recs == [] and "HTTP 429" in sp.run_stats["x_stop"]
    assert _rows(owner)["@alice"]["status"] == "new"  # retried next run
    fx.fail["/2/users/by"] = httpx.Response(400, json={"title": "Invalid Request", "detail": "bad usernames"})
    sp, recs, _ = _run(fx, ctx_factory, db, env={"X_SPIDER_PHASES": "lookup"})
    row = _rows(owner)["@alice"]
    assert row["status"] == "failed" and "bad usernames" in row["payload"]["lookup_error"]


def test_lookup_limit_leaves_unwritten_rows_new(db, owner, ctx_factory):
    fx = _world()
    _queue(db, "alice", "acmehq", score_first="acmehq")
    sp, recs, _ = _run(fx, ctx_factory, db, env={"X_SPIDER_PHASES": "lookup"}, limit=1)
    assert len(recs) == 1 and "limit_stop" in sp.run_stats
    rows = _rows(owner)
    assert rows["@acmehq"]["status"] == "fetched" and rows["@alice"]["status"] == "new"


def test_lookup_errors_parsing():
    assert lookup_errors(
        {
            "errors": [
                {"value": "Ghost", "detail": "Could not find user with usernames: [Ghost]."},
                {"resource_id": "bad", "detail": "User has been suspended: [bad]."},
                {"title": "no value"},
                "junk",
            ]
        }
    ) == {
        "ghost": "not found: Could not find user with usernames: [Ghost].",
        "bad": "suspended: User has been suspended: [bad].",
    }


def test_lookup_through_the_pipeline(deps, owner):
    """End to end: the looked-up account becomes an entity (written as the x-spider worker)."""
    owner.execute("truncate frontier")
    fx = _world()
    _queue(deps.db, "alice")
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "lookup"}
    stats = run_sync(XSpider(transport=fx.transport(), sleep=lambda s: None, seeds={}), deps)
    assert stats.fetched == 1 and stats.minted == 1
    ent = owner.execute(
        "select c.subject, c.worker_id from claim c where c.predicate = 'x_id' and c.value_text = '101'"
    ).fetchone()
    assert ent["subject"].startswith("rbx:person:") and ent["worker_id"].startswith("x-spider-spider-")
    run = owner.execute("select stats from run where worker_id = %s", (ent["worker_id"],)).fetchone()
    assert run["stats"]["lookup"] == {"found": 1} and run["stats"]["user_reads"] == 1


@pytest.mark.parametrize("phases", ["following", "posts", "expansion"])
def test_crawl_only_phases_skip_lookups(db, owner, ctx_factory, phases):
    fx = _world()
    _queue(db, "alice")
    _run(fx, ctx_factory, db, env={"X_SPIDER_PHASES": phases})
    assert fx.lookups == [] and _rows(owner)["@alice"]["status"] == "new"
