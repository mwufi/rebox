"""Worker identity, runs and evidence (CONTRACTS §12).

The first group runs against the in-process fake writer. The second group starts the real API
from this checkout (``apps/api``, via its local ``tsx``) on the test database, so the worker's
requests are checked against the real writer, fold and read API; it is skipped if the API's
node_modules are not installed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from rebox_worker import cli
from rebox_worker.client import ClaimsClient
from rebox_worker.connectors import get_connector
from rebox_worker.connectors.base import Ctx
from rebox_worker.identity import WorkerSpec, spec_for, worker_name
from rebox_worker.models import ClaimInput, EntityRef, Evidence, RawRecord
from rebox_worker.pipeline import Deps, run_replay, run_sync
from rebox_worker.rawstore import FileRawStore
from rebox_worker.resolve import Resolver

REPO = Path(__file__).resolve().parents[3]
API_DIR = REPO / "apps" / "api"
WORKER_RE = r"^example-connector-\d{6}$"


# ── identity ──


def test_worker_names_and_default_spec():
    assert worker_name("x_spider") == "x-spider"
    assert worker_name("  DBooth!! ") == "dbooth"
    with pytest.raises(ValueError):
        worker_name("__")
    spec = spec_for(get_connector("hn"))
    assert (spec.name, spec.kind, spec.source, spec.layers) == ("hn", "connector", "hn", ("public/hn",))
    with pytest.raises(ValueError):
        WorkerSpec(name="ext", kind="external")

    class Spider:
        name, layer, interval_s = "x_spider", "public/x_spider", 60

        def worker_spec(self, ctx):
            return WorkerSpec(name="dbooth", kind="spider", config={"seeds": ["davidbooth"]})

    s = spec_for(Spider())
    assert (s.name, s.kind, s.source, s.layers, dict(s.config)) == (
        "dbooth",
        "spider",
        "x_spider",
        ("public/x_spider",),
        {"seeds": ["davidbooth"]},
    )


def test_sync_registers_once_and_tracks_runs(deps, owner, writer):
    stats = run_sync(get_connector("example"), deps)
    workers = owner.execute("select id, kind, source, trust, layers, last_seen_at from worker").fetchall()
    assert len(workers) == 1
    w = workers[0]
    assert __import__("re").match(WORKER_RE, w["id"])
    assert (w["kind"], w["source"], w["layers"], w["last_seen_at"] is not None) == (
        "connector",
        "example",
        ["public/example"],
        True,
    )
    # Every claim is attributed to the worker and the run.
    run = owner.execute("select * from run").fetchone()
    assert (run["worker_id"], run["kind"], run["status"]) == (w["id"], "sync", "ok")
    assert run["finished_at"] is not None and run["error"] is None
    assert run["stats"] == stats.as_dict() and run["stats"]["written"] == 35
    attributed = owner.execute(
        "select count(*) filter (where worker_id = %s and run_id = %s) a, count(*) n from claim",
        (w["id"], run["id"]),
    ).fetchone()
    assert attributed["a"] == attributed["n"] == 35
    assert all(r["worker"] == w["id"] and r["run_id"] == run["id"] for r in writer.requests)

    # Second run: same identity (no new worker row), a new run; replay is its own run kind.
    run_sync(get_connector("example"), deps)
    run_replay(get_connector("example"), deps)
    assert owner.execute("select count(*) n from worker").fetchone()["n"] == 1
    runs = owner.execute("select kind, status, stats from run order by id").fetchall()
    assert [(r["kind"], r["status"]) for r in runs] == [("sync", "ok"), ("sync", "ok"), ("replay", "ok")]
    assert runs[1]["stats"]["skipped_unchanged"] == 5 and runs[2]["stats"]["fetched"] == 5
    # Identity is persisted in the worker table, so losing connector_state keeps it.
    owner.execute("delete from connector_state")
    run_sync(get_connector("example"), deps)
    assert owner.execute("select count(distinct worker_id) n from run").fetchone()["n"] == 1


def test_failed_sync_closes_run_as_failed(deps, owner, writer):
    writer.fail_next = [400]
    deps.client.max_retries = 0
    with pytest.raises(Exception):  # noqa: B017
        run_sync(get_connector("example"), deps)
    run = owner.execute("select status, error, finished_at, stats from run").fetchone()
    assert run["status"] == "failed" and run["finished_at"] is not None
    assert run["error"].startswith("WriterError") or run["error"].startswith("ValidationRejected")
    assert run["stats"]["fetched"] >= 1


def test_usage_and_per_record_workers(deps, owner, writer):
    """A connector may write some records as other identities (one run each) and report usage."""
    from rebox_worker.models import CursorUpdate

    class Seeded:
        name, layer, interval_s = "seeded", "public/seeded", 60

        def fetch(self, ctx, cursor):
            for seed in ("dbooth", "yoni", "dbooth"):
                ctx.add_usage(api_calls=1, cost_usd=0.01)
                yield RawRecord.new(
                    "seeded", f"{seed}:{len(ctx.usage)}:{ctx.usage['api_calls']}", {"seed": seed}
                )
            yield CursorUpdate(cursor={"done": True})

        def worker_for(self, rec):
            return WorkerSpec(
                name=rec.payload["seed"], kind="spider", config={"seeds": [rec.payload["seed"]]}
            )

        def extract(self, rec):
            ref = EntityRef(
                kind="person", identity={"x_handle": rec.payload["seed"]}, name=rec.payload["seed"]
            )
            return [
                ClaimInput(
                    subject=ref, predicate="x_handle", value=rec.payload["seed"], source_ref=rec.external_id
                )
            ]

    stats = run_sync(Seeded(), deps)
    assert stats.as_dict()["api_calls"] == 3 and stats.as_dict()["cost_usd"] == pytest.approx(0.03)
    workers = {r["id"].rsplit("-", 2)[0]: r for r in owner.execute("select id, kind, config from worker")}
    assert set(workers) == {"seeded", "dbooth", "yoni"}
    assert workers["dbooth"]["kind"] == "spider" and workers["dbooth"]["config"] == {"seeds": ["dbooth"]}
    runs = {
        r["worker_id"].rsplit("-", 2)[0]: r
        for r in owner.execute("select worker_id, kind, status, stats from run")
    }
    assert {k: r["status"] for k, r in runs.items()} == {"seeded": "ok", "dbooth": "ok", "yoni": "ok"}
    # Fetch-level stats and usage go to the main run; write stats to the run that wrote them.
    assert runs["seeded"]["stats"]["api_calls"] == 3 and runs["seeded"]["stats"]["written"] == 0
    assert runs["dbooth"]["stats"]["claims"] > runs["yoni"]["stats"]["claims"] > 0
    by_worker = owner.execute(
        "select split_part(worker_id, '-', 1) w, count(*) n from claim group by 1 order by 1"
    ).fetchall()
    assert [r["w"] for r in by_worker] == ["dbooth", "yoni"]


def test_evidence_and_json_values_through_resolver(deps, writer, owner):
    resolver = Resolver(deps.db, source="t", layer="public/t")
    co = EntityRef(kind="company", identity={"domain": "acme.example"}, name="Acme")
    ev = [Evidence(url="https://x.com/acme/status/1", author="acme", quote="We raised a $4M seed")]
    claims = [
        ClaimInput(
            subject=co,
            predicate="milestone",
            value={"title": "Raised a $4M seed", "kind": "funding", "url": "https://x.com/acme/status/1"},
            valid_from="2026-03-01",
            source_ref="t:ms:1",
            evidence=ev,
        ),
        ClaimInput(subject=co, predicate="description", value={"bad": 1}, source_ref="t:bad"),
    ]
    res = resolver.resolve(claims)
    assert [why for _, why in res.rejected] == ["attribute 'description' value must be a scalar or array"]
    ms = next(c for c in res.claims if c.predicate == "milestone")
    assert ms.to_request()["evidence"] == [
        {"url": "https://x.com/acme/status/1", "author": "acme", "quote": "We raised a $4M seed"}
    ]
    # The generated name of a minted entity cites the evidence of the claim that minted it, so a
    # parser's whole batch (including generated names) satisfies "evidence required".
    name = next(c for c in res.claims if c.predicate == "name")
    assert name.to_request()["evidence"] == ms.to_request()["evidence"]
    wid = deps.client.register_worker(name="xpost", kind="parser", source="t", layers=["public/t"])
    deps.client.write("public/t", res.claims, worker=wid)
    with pytest.raises(Exception, match="evidence required"):  # parsers must cite evidence
        deps.client.write("public/t", [name.model_copy(update={"evidence": None})], worker=wid)
    row = owner.execute("select value, evidence from claim where predicate = 'milestone'").fetchone()
    assert row["value"]["kind"] == "funding" and row["evidence"][0]["quote"] == "We raised a $4M seed"
    with pytest.raises(Exception, match="url"):
        Evidence(url="ftp://nope")


def test_cli_workers_list_and_show(env, owner, capsys):
    assert cli.main(["workers", "list"]) == 0
    assert "no workers yet" in capsys.readouterr().out
    assert cli.main(["workers", "register", "example"]) == 0
    wid = capsys.readouterr().out.strip()
    assert __import__("re").match(WORKER_RE, wid)
    assert cli.main(["workers", "register", "example"]) == 0
    assert capsys.readouterr().out.strip() == wid
    assert cli.main(["sync", "example"]) == 0
    capsys.readouterr()
    assert cli.main(["workers", "list"]) == 0
    out = capsys.readouterr().out
    assert wid in out and "connector" in out and " 35 " in out
    assert cli.main(["workers", "show", wid]) == 0
    out = capsys.readouterr().out
    assert "sync" in out and " ok " in out and '"written": 35' in out
    assert cli.main(["workers", "show", "nope-connector-000000"]) == 1


@pytest.fixture
def env(monkeypatch, app_url, writer, tmp_path, db):
    monkeypatch.setenv("DATABASE_URL", app_url)
    monkeypatch.setenv("API_URL", writer.url)
    monkeypatch.setenv("INGEST_TOKEN", writer.token)
    monkeypatch.setenv("RAW_STORE", f"file://{tmp_path}/raw")
    monkeypatch.delenv("FIXTURES_DIR", raising=False)


# ── against the real API ──


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def real_api(app_url: str) -> Iterator[str]:
    tsx = API_DIR / "node_modules" / ".bin" / "tsx"
    if not tsx.exists():
        pytest.skip("apps/api/node_modules not installed (pnpm install)")
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": app_url,
        "PORT": str(port),
        "INGEST_TOKEN": "test-ingest-token",
        "ADMIN_TOKEN": "test-admin-token",
        "REBOX_ENV": "dev",
    }
    proc = subprocess.Popen(
        [str(tsx), "src/server.ts"], cwd=API_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(
                    f"api did not start: {proc.stderr.read().decode()[-2000:] if proc.stderr else ''}"
                )
            time.sleep(0.3)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def api_deps(real_api: str, db, tmp_path) -> Iterator[Deps]:
    c = ClaimsClient(real_api, "test-ingest-token", backoff_s=0.01, max_backoff_s=0.05)
    yield Deps(
        db=db, raw=FileRawStore(tmp_path / "raw", db), client=c, ctx=Ctx(fixtures=REPO / "fixtures" / "raw")
    )
    c.close()


def _corroborating_connector(name: str, kind: str = "connector"):
    """A connector that states one fact: Ada worked at Acme (CTO), with evidence."""

    class C:
        layer, interval_s = f"public/{name}", 60
        worker_kind = kind

        def fetch(self, ctx, cursor):
            yield RawRecord.new(name, "ada", {"n": 1})

        def extract(self, rec):
            ada = EntityRef(kind="person", identity={"github": "ada-prov"}, name="Ada Prov")
            acme = EntityRef(kind="company", identity={"domain": "acme-prov.example"}, name="Acme Prov")
            ev = [Evidence(url=f"https://{name}.example/ada", author=name, quote="Ada, CTO at Acme")]
            return [
                ClaimInput(
                    subject=ada,
                    predicate="worked_at",
                    object=acme,
                    value={"title": "CTO"},
                    source_ref=f"{name}:ada:acme",
                    evidence=ev,
                ),
                ClaimInput(
                    subject=acme,
                    predicate="milestone",
                    value={"title": "Acme launched", "kind": "launch", "url": "https://acme-prov.example"},
                    valid_from="2026-10-01",
                    source_ref=f"{name}:acme:ms",
                    evidence=ev,
                ),
            ]

    C.name = name
    return C()


def test_two_workers_corroborate_against_real_api(api_deps, real_api, owner):
    """End to end: two workers write the same fact -> support = 2; runs close with stats; the
    read API shows worker, evidence and the milestone; /v0/workers lists both."""
    s1 = run_sync(_corroborating_connector("alpha"), api_deps)
    s2 = run_sync(_corroborating_connector("beta", kind="parser"), api_deps)
    # alpha mints Ada and Acme: two generated names + worked_at + milestone; beta matches both
    # (same deterministic ids) and writes only its own worked_at + milestone.
    assert (s1.written, s1.minted, s2.written, s2.matched) == (4, 2, 2, 2)
    ada = owner.execute(
        "select subject from claim where predicate = 'name' and value_text = 'Ada Prov'"
    ).fetchone()
    view = httpx.get(f"{real_api}/v0/entities/{ada['subject']}").json()
    worked = [f for f in view["facts"] if f["predicate"] == "worked_at"]
    assert len(worked) == 1 and worked[0]["support"] == 2
    # Same scope: the connector (trust 0.8) beats the newer parser claim (0.6).
    assert worked[0]["worker"].startswith("alpha-connector-")
    assert worked[0]["evidence"] == [
        {"url": "https://alpha.example/ada", "author": "alpha", "quote": "Ada, CTO at Acme"}
    ]
    acme = worked[0]["object"]["id"]
    ms = [
        f
        for f in httpx.get(f"{real_api}/v0/entities/{acme}").json()["facts"]
        if f["predicate"] == "milestone"
    ]
    assert len(ms) == 1 and ms[0]["support"] == 2 and ms[0]["value"]["kind"] == "launch"

    runs = owner.execute(
        "select w.kind, r.status, r.stats, r.finished_at"
        " from run r join worker w on w.id = r.worker_id order by r.id"
    ).fetchall()
    assert [(r["kind"], r["status"]) for r in runs] == [("connector", "ok"), ("parser", "ok")]
    assert all(r["finished_at"] is not None and r["stats"]["written"] > 0 for r in runs)
    listed = {w["id"].split("-")[0]: w for w in httpx.get(f"{real_api}/v0/workers").json()}
    assert listed["alpha"]["runs_ok"] == 1 and listed["alpha"]["claims_total"] == s1.written
    assert listed["beta"]["last_run"]["status"] == "ok" and listed["beta"]["trust"] == pytest.approx(0.6)

    # The parser writing without evidence is refused claim by claim by the real writer; since S12
    # the run drops those claims, counts them as rejected, and still closes ok.
    class NoEvidence:
        name, layer, interval_s, worker_kind = "beta", "public/beta", 60, "parser"

        def fetch(self, ctx, cursor):
            yield RawRecord.new("beta", "x", {"n": 2})

        def extract(self, rec):
            return [
                ClaimInput(
                    subject=EntityRef(kind="person", identity={"github": "nobody-prov"}, name="N"),
                    predicate="github",
                    value="nobody-prov",
                    source_ref="b:x",
                )
            ]

    stats = run_sync(NoEvidence(), api_deps)
    assert (stats.written, stats.rejected) == (0, 2)  # the github claim and the generated name
    last = owner.execute("select status, error, stats from run order by id desc limit 1").fetchone()
    assert last["status"] == "ok" and last["error"] is None and last["stats"]["rejected"] == 2


def test_real_api_partial_rejection_of_500(api_deps, owner):
    """S12 against the real writer: one non-normalized identity value among 500 claims."""
    from rebox_worker.models import ClaimWrite

    c = api_deps.client
    wid = c.register_worker(name="s12", kind="connector", source="s12", layers=["public/s12"])
    claims = [
        ClaimWrite(
            subject={"mint": {"id": f"rbx:company:s12-{i}", "kind": "company"}},
            predicate="website",
            value="http://WWW.com/"
            if i == 250
            else ("http://www.com" if i == 0 else f"https://c{i}.example"),
            source_ref=f"s12:{i}",
        )
        for i in range(500)
    ]
    res = c.write("public/s12", claims, worker=wid, drop_rejected=True)
    assert (res.written, res.minted) == (499, 499)
    assert [(r.claim.source_ref, r.reason) for r in res.rejected] == [
        ("s12:250", 'identity value "http://WWW.com/" is not normalized (url): expected "http://www.com"')
    ]
    assert owner.execute("select count(*) n from claim where layer_id = 'public/s12'").fetchone()["n"] == 499


# ── S8 hooks: per-seed workers (workers / worker_for) and connector run_stats ──


def test_x_spider_writes_each_seed_set_as_its_own_worker(deps, owner, writer):
    """XSpider.workers() -> one spider worker per seed set, all registered up front; each
    x_following page is written as its seed set's worker (in that worker's run); profiles and
    expansion pages as the connector-level worker; no batch mixes workers."""
    from spiders.test_x_fetch import ENV, world

    from rebox_worker.connectors.x_spider import XSpider

    owner.execute("truncate frontier")
    fx = world()
    sp = XSpider(
        transport=fx.transport(),
        sleep=lambda s: None,
        seeds={"dbooth": ["seed_a"], "yoni": ["seed_b"], "sf-core": []},
    )
    deps.ctx.env = {**ENV, "X_SPIDER_PHASES": "following"}
    stats = run_sync(sp, deps)

    workers = {
        r["id"].rsplit("-", 2)[0]: r
        for r in owner.execute("select id, kind, source, config, layers from worker")
    }
    # Every seed set is registered (sf-core too, though it wrote nothing), plus the connector-level worker.
    assert set(workers) == {"dbooth", "yoni", "sf-core", "x-spider"}
    assert all(w["kind"] == "spider" and w["source"] == "x_spider" for w in workers.values())
    # x_spider's table trust (config/trust.yaml, 0.75) differs from the spider default: recorded.
    assert workers["dbooth"]["config"] == {"seeds": ["seed_a"], "trust_source": "table", "trust_table": 0.75}
    assert workers["yoni"]["layers"] == ["public/x_spider"]
    import re

    assert re.fullmatch(r"dbooth-spider-\d{6}", workers["dbooth"]["id"])

    # Batches never mix workers, and each carries its worker's own run.
    runs = {r["id"]: r for r in owner.execute("select id, worker_id, kind, status, stats from run")}
    for body in writer.requests:
        assert runs[body["run_id"]]["worker_id"] == body["worker"]
    # follows edges (from x_following pages) are attributed to the seed set that followed.
    follows = owner.execute(
        "select regexp_replace(worker_id, '-spider-[0-9]{6}$', '') w, count(*) n from claim"
        " where predicate = 'follows' group by 1"
    ).fetchall()
    assert {r["w"]: r["n"] for r in follows} == {"dbooth": 5, "yoni": 4}
    # Profiles (x_user records) are the connector-level worker's.
    assert {
        r["w"]
        for r in owner.execute(
            "select distinct regexp_replace(worker_id, '-spider-[0-9]{6}$', '') w"
            " from claim where predicate = 'x_id'"
        )
    } == {"x-spider"}

    by_worker = {r["worker_id"].rsplit("-", 2)[0]: r for r in runs.values()}
    assert set(by_worker) == {"x-spider", "dbooth", "yoni"}  # sf-core wrote nothing: no run
    assert all(r["status"] == "ok" for r in by_worker.values())
    assert by_worker["dbooth"]["stats"]["written"] == 5 and by_worker["yoni"]["stats"]["written"] == 4
    # The connector's run_stats are merged into the connector-level run (and returned to the CLI).
    main = by_worker["x-spider"]["stats"]
    assert main["user_reads"] == fx.user_objects and main["api_calls"] == len(fx.calls) == 3
    assert main["cost_usd"] == pytest.approx(fx.cost) and "x_total_budget_usd" in main
    assert "user_reads" not in by_worker["dbooth"]["stats"]
    assert stats.as_dict()["api_calls"] == 3


def test_worker_for_names_and_specs_and_run_stats_on_failure(deps, owner, writer):
    """worker_for may return a workers() name, a dict, or None; run_stats (attribute or method)
    is merged even when the run fails."""
    from rebox_worker.models import CursorUpdate

    class Multi:
        name, layer, interval_s, worker_kind = "multi", "public/multi", 60, "spider"

        def __init__(self, fail: bool = False):
            self.fail = fail
            self.run_stats = {}

        def workers(self, ctx):
            return [{"name": "alpha", "config": {"seeds": ["a"]}}, {"name": "multi", "description": "main"}]

        def worker_for(self, rec):
            return {"a": "alpha", "b": {"name": "beta", "kind": "parser"}}.get(rec.payload["w"])

        def fetch(self, ctx, cursor):
            for w in ("a", "b", "c", "a"):
                yield RawRecord.new("multi", f"{w}:{ctx.usage.get('n', 0)}", {"w": w, "n": len(w)})
                ctx.add_usage(n=1)
            self.run_stats = {"api_calls": 4, "x_stop": "429 Too Many Requests"}
            if self.fail:
                raise RuntimeError("upstream broke")
            yield CursorUpdate(cursor={"ok": True})

        def extract(self, rec):
            ev = [Evidence(url="https://example.com/x")]
            h = rec.external_id.replace(":", "-")
            ref = EntityRef(kind="person", identity={"x_handle": h}, name=h)
            return [
                ClaimInput(
                    subject=ref, predicate="x_handle", value=h, source_ref=rec.external_id, evidence=ev
                )
            ]

    run_sync(Multi(), deps)
    workers = {
        r["id"].rsplit("-", 2)[0]: r for r in owner.execute("select id, kind, description from worker")
    }
    assert {k: w["kind"] for k, w in workers.items()} == {
        "alpha": "spider",
        "multi": "spider",
        "beta": "parser",
    }
    assert workers["multi"]["description"] == "main"  # the listed entry is the connector-level identity
    runs = {
        r["w"]: r
        for r in owner.execute(
            "select split_part(worker_id, '-', 1) w, kind, status, stats from run order by id"
        )
    }
    assert runs["beta"]["kind"] == "parse" and runs["alpha"]["kind"] == "sync"
    assert runs["alpha"]["stats"]["claims"] == 4  # two records, each a claim + a generated name
    assert runs["multi"]["stats"]["x_stop"] == "429 Too Many Requests" and runs["multi"]["stats"]["n"] == 4
    for body in writer.requests:
        assert len({body["worker"]}) == 1

    owner.execute("truncate run cascade")
    with pytest.raises(RuntimeError, match="upstream broke"):
        run_sync(Multi(fail=True), deps)
    failed = owner.execute("select status, error, stats from run where worker_id like 'multi-%'").fetchone()
    assert failed["status"] == "failed" and "upstream broke" in failed["error"]
    assert failed["stats"]["api_calls"] == 4 and failed["stats"]["x_stop"].startswith("429")
