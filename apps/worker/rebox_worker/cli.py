"""``rebox-worker`` command line.

rebox-worker run [--once]          claim and run queued jobs (loop)
rebox-worker sync <source>         run a connector now, inline
rebox-worker replay <source>       re-extract a source from the raw store
    [--retract-stale [--dry-run] [--force]]   ... then retract claims it no longer emits (S19)
rebox-worker schedule-due          enqueue syncs for connectors that are due
rebox-worker enqueue <source>      queue a sync (or --kind replay)
rebox-worker status                connector_state table
rebox-worker workers list          worker identities with claim and run counts
rebox-worker workers register <c>  resolve (create on first use) a connector's worker id
rebox-worker workers show <id>     one worker and its recent runs
rebox-worker coverage FILE.csv     which rows of a list Rebox knows (S14)
rebox-worker export --layers L,... --out FILE    write layers to a bundle (.rebox.jsonl.gz)
rebox-worker import FILE [--dry-run]             merge a bundle in through the resolver
rebox-worker raw reindex <source>  index raw files a crashed run left out of raw_record (S16)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Any

from . import __version__, coverage, exchange, identity, jobs, rawstore
from .client import ClaimsClient
from .config import ConfigError, Settings
from .connectors import REGISTRY, Ctx, UnknownConnector, get_connector
from .db import Db
from .dedupe import add_parser as add_dedupe_parser
from .pipeline import Deps, RetractStale, SourceBusy, Stats, replay_retract_stale, run_replay, run_sync
from .rawstore import RawStore

log = logging.getLogger("rebox_worker")


class UnknownJobKind(ValueError):
    pass


# ── wiring ────────────────────────────────────────────────────────────────


@contextmanager
def open_deps(settings: Settings, *, limit: int | None = None, need_writer: bool = True):
    db = Db(settings.database_url)
    client: Any = None
    try:
        if need_writer:
            api_url, token = settings.require_writer()
            client = ClaimsClient(api_url, token, user_agent=settings.user_agent)
        yield Deps(
            db=db,
            raw=RawStore.from_url(settings.raw_store, db),
            client=client,
            ctx=Ctx(user_agent=settings.user_agent, limit=limit, env=settings.extra, db=db),
            lock_url=settings.database_url,
        )
    finally:
        if client is not None:
            client.close()
        db.close()


def run_job(job: jobs.Job, deps: Deps) -> Stats:
    connector = get_connector(job.source)
    if job.kind == "sync":
        return run_sync(connector, deps)
    if job.kind == "replay":
        return run_replay(connector, deps)
    raise UnknownJobKind(f"unknown job kind {job.kind!r}")


def work_once(deps: Deps) -> bool:
    """Claim and run one job. False if the queue had nothing ready."""
    job = jobs.claim(deps.db)
    if job is None:
        return False
    log.info("job %d: %s %s (attempt %d/%d)", job.id, job.kind, job.source, job.attempts, job.max_attempts)
    try:
        stats = run_job(job, deps)
    except (UnknownConnector, UnknownJobKind) as e:  # retrying will not help
        status = jobs.mark_failed(deps.db, job, f"{type(e).__name__}: {e}", retry=False)
        log.error("job %d: %s (%s)", job.id, e, status)
    except SourceBusy as e:
        status = jobs.mark_failed(deps.db, job, str(e))
        log.warning("job %d: %s (%s)", job.id, e, status)
    except Exception as e:
        status = jobs.mark_failed(deps.db, job, f"{type(e).__name__}: {e}")
        log.exception("job %d failed (%s)", job.id, status)
    else:
        jobs.mark_done(deps.db, job)
        log.info("job %d: done %s", job.id, json.dumps(stats.as_dict()))
    return True


# ── commands ──────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    stop = False

    def _stop(signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True
        log.info("received signal %d; finishing the current job", signum)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    with open_deps(settings) as deps:
        jobs.requeue_stale(deps.db)
        if args.once:
            while work_once(deps) and not args.single:
                pass
            return 0
        log.info("worker started; polling every %.0fs", args.poll)
        while not stop:
            if not work_once(deps):
                jobs.requeue_stale(deps.db)
                deadline = time.monotonic() + args.poll
                while not stop and time.monotonic() < deadline:
                    time.sleep(0.2)
    return 0


def cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    connector = get_connector(args.source)
    with open_deps(settings, limit=args.limit) as deps:
        stats = run_sync(connector, deps)
    print(f"sync {args.source}: " + json.dumps(stats.as_dict()))
    return 0


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    connector = get_connector(args.source)
    if not args.retract_stale:
        if args.dry_run or args.force:
            print("rebox-worker: --dry-run and --force go with --retract-stale", file=sys.stderr)
            return 2
        with open_deps(settings) as deps:
            stats = run_replay(connector, deps)
        print(f"replay {args.source}: " + json.dumps(stats.as_dict()))
        return 0
    opts = RetractStale(dry_run=args.dry_run, force=args.force, examples=args.examples)
    with open_deps(settings, need_writer=not args.dry_run) as deps:
        report = replay_retract_stale(connector, deps, opts)
    if report.replay is not None:
        print(f"replay {args.source}: " + json.dumps(report.replay))
    print(report.render())
    return 1 if report.refused else 0


def cmd_schedule_due(args: argparse.Namespace, settings: Settings) -> int:
    with Db(settings.database_url, max_size=1) as db:
        ctx = Ctx(user_agent=settings.user_agent, env=os.environ, db=db)
        enqueued = jobs.schedule_due(db, {name: cls() for name, cls in REGISTRY.items()}, ctx)
    print("schedule-due: enqueued " + (", ".join(enqueued) if enqueued else "nothing"))
    return 0


def cmd_enqueue(args: argparse.Namespace, settings: Settings) -> int:
    get_connector(args.source)  # fail fast on a typo
    with Db(settings.database_url, max_size=1) as db:
        job_id = jobs.enqueue(db, args.kind, args.source, max_attempts=args.max_attempts)
    print(f"enqueued job {job_id}: {args.kind} {args.source}")
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    with Db(settings.database_url, max_size=1) as db, db.view() as conn:
        rows = conn.execute(
            """select s.source, s.interval_s, s.next_run_at, s.last_run_at, s.last_status, s.last_stats,
                      s.cursor,
                      (select count(*) from job j where j.payload->>'source' = s.source
                          and j.status in ('queued','running')) as active_jobs
                 from connector_state s order by s.source"""
        ).fetchall()
    if args.json:
        print(json.dumps(rows, default=str, indent=1))
        return 0
    if not rows:
        print("no connector state yet (run `rebox-worker sync <source>` or `schedule-due`)")
        return 0
    cols = ["source", "interval_s", "next_run_at", "last_run_at", "last_status", "active_jobs", "cursor"]
    table = [[_cell(r[c]) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in table)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)))
    for row in table:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))
    for r in rows:
        if r["last_stats"]:
            print(f"{r['source']} last_stats: {json.dumps(r['last_stats'], sort_keys=True)}")
    return 0


def cmd_workers_list(args: argparse.Namespace, settings: Settings) -> int:
    with Db(settings.database_url, max_size=1) as db, db.view() as conn:
        rows = conn.execute(
            """select w.id, w.kind, w.source, w.owner, w.trust, w.status, w.last_seen_at,
                      (select count(distinct ch.claim_id) from change ch
                        where ch.worker_id = w.id and ch.op in ('insert', 'update')) as claims,
                      (select count(*) from run r where r.worker_id = w.id and r.status = 'ok') as runs_ok,
                      (select count(*) from run r
                        where r.worker_id = w.id and r.status = 'failed') as runs_failed
                 from worker w order by w.last_seen_at desc nulls last, w.id"""
        ).fetchall()
    if args.json:
        print(json.dumps(rows, default=str, indent=1))
        return 0
    if not rows:
        print("no workers yet (they register on their first sync, or `rebox-worker workers register <c>`)")
        return 0
    _table(
        rows,
        [
            "id",
            "kind",
            "source",
            "owner",
            "trust",
            "status",
            "last_seen_at",
            "claims",
            "runs_ok",
            "runs_failed",
        ],
    )
    return 0


def cmd_workers_register(args: argparse.Namespace, settings: Settings) -> int:
    connector = get_connector(args.connector)
    api_url, token = settings.require_writer()
    client = ClaimsClient(api_url, token, user_agent=settings.user_agent)
    try:
        spec = identity.spec_for(connector, Ctx(user_agent=settings.user_agent, env=settings.extra))
        print(identity.register(client, spec))
    finally:
        client.close()
    return 0


def cmd_workers_show(args: argparse.Namespace, settings: Settings) -> int:
    with Db(settings.database_url, max_size=1) as db, db.view() as conn:
        w = conn.execute(
            """select id, kind, source, owner, trust, status, layers, config, description, created_at,
                      last_seen_at from worker where id = %s""",
            (args.id,),
        ).fetchone()
        if w is None:
            print(f"rebox-worker: no worker {args.id!r}", file=sys.stderr)
            return 1
        runs = conn.execute(
            """select id, kind, status, started_at, finished_at,
                      round(extract(epoch from (coalesce(finished_at, now()) - started_at))::numeric, 1)
                        as duration_s,
                      stats, error
                 from run where worker_id = %s order by started_at desc, id desc limit %s""",
            (args.id, args.runs),
        ).fetchall()
    if args.json:
        print(json.dumps({**w, "runs": runs}, default=str, indent=1))
        return 0
    for k, v in w.items():
        print(f"{k:>13}: {_cell(v)}")
    print()
    if runs:
        _table(runs, ["id", "kind", "status", "started_at", "duration_s", "error"])
        for r in runs:
            if r["stats"]:
                print(f"run {r['id']} stats: {json.dumps(r['stats'], sort_keys=True)}")
    else:
        print("no runs yet")
    return 0


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    layers = [x.strip() for x in args.layers.split(",") if x.strip()]
    with Db(settings.database_url, max_size=1) as db:
        summary = exchange.export_layers(db, layers, args.out, source_instance=args.instance)
    print(summary.render())
    return 0


def cmd_import(args: argparse.Namespace, settings: Settings) -> int:
    opts = exchange.ImportOptions(
        dry_run=args.dry_run,
        layers=[x.strip() for x in args.layers.split(",") if x.strip()] if args.layers else None,
        match_website=args.match_website,
        batch_claims=args.batch,
        examples=args.examples,
    )
    client: ClaimsClient | None = None
    if not args.dry_run:
        api_url, token = settings.require_writer()
        client = ClaimsClient(api_url, token, user_agent=settings.user_agent)
    try:
        with Db(settings.database_url, max_size=2) as db:
            try:
                report = exchange.import_bundle(args.file, db, client, opts)
            except exchange.ImportAborted as e:
                print(f"rebox-worker: import aborted, nothing written: {e}", file=sys.stderr)
                return 1
    finally:
        if client is not None:
            client.close()
    print(json.dumps(report.as_dict(), indent=1, default=str) if args.json else report.render())
    return 1 if report.aborted else 0


def _table(rows: Sequence[dict[str, Any]], cols: list[str]) -> None:
    table = [[_cell(r[c]) for c in cols] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in table)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)).rstrip())
    for row in table:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip())


def _cell(v: Any) -> str:
    if v is None:
        return "-"
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d %H:%M:%S%z")
    if isinstance(v, dict | list):
        return json.dumps(v, sort_keys=True)
    return str(v)


# ── argument parsing ──────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rebox-worker",
        description="Rebox worker: connectors -> raw store -> resolver -> claims writer.",
        epilog=f"connectors: {', '.join(sorted(REGISTRY))}. "
        "Env: DATABASE_URL (app role), API_URL, INGEST_TOKEN, RAW_STORE, USER_AGENT, FIXTURES_DIR.",
    )
    p.add_argument("--version", action="version", version=f"rebox-worker {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    r = sub.add_parser("run", help="claim and run queued jobs")
    r.add_argument("--once", action="store_true", help="drain ready jobs, then exit")
    r.add_argument("--single", action="store_true", help="with --once: run at most one job")
    r.add_argument("--poll", type=float, default=5.0, help="seconds between polls when idle")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("sync", help="run a connector now (inline)")
    s.add_argument("source")
    s.add_argument("--limit", type=int, help="stop after N raw records")
    s.set_defaults(func=cmd_sync)

    rp = sub.add_parser("replay", help="re-extract a source from the raw store (no fetch)")
    rp.add_argument("source")
    rp.add_argument(
        "--retract-stale",
        action="store_true",
        help="then retract the source's claims the replay no longer emits (as their workers)",
    )
    rp.add_argument("--dry-run", action="store_true", help="with --retract-stale: report only (no replay)")
    rp.add_argument("--force", action="store_true", help="with --retract-stale: allow > 30%% of a layer")
    rp.add_argument("--examples", type=int, default=10, help="stale examples to print")
    rp.set_defaults(func=cmd_replay)

    sd = sub.add_parser("schedule-due", help="enqueue syncs for connectors that are due")
    sd.set_defaults(func=cmd_schedule_due)

    e = sub.add_parser("enqueue", help="queue a job for a source")
    e.add_argument("source")
    e.add_argument("--kind", choices=jobs.JOB_KINDS, default="sync")
    e.add_argument("--max-attempts", type=int, default=3)
    e.set_defaults(func=cmd_enqueue)

    st = sub.add_parser("status", help="show connector_state")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    w = sub.add_parser("workers", help="worker identities and their runs (CONTRACTS §12)")
    wsub = w.add_subparsers(dest="workers_command", required=True, metavar="subcommand")
    wl = wsub.add_parser("list", help="list workers with claim and run counts")
    wl.add_argument("--json", action="store_true")
    wl.set_defaults(func=cmd_workers_list)
    wr = wsub.add_parser("register", help="resolve (and create on first use) a connector's worker id")
    wr.add_argument("connector")
    wr.set_defaults(func=cmd_workers_register)
    ws = wsub.add_parser("show", help="one worker and its recent runs")
    ws.add_argument("id")
    ws.add_argument("--runs", type=int, default=10, help="how many recent runs to show")
    ws.add_argument("--json", action="store_true")
    ws.set_defaults(func=cmd_workers_show)

    coverage.add_parser(sub)  # rebox-worker coverage FILE.csv (S14)
    ex = sub.add_parser(
        "export", help="write whole layers to a bundle (.rebox.jsonl.gz) for another instance"
    )
    ex.add_argument(
        "--layers", required=True, help="comma-separated layer ids, e.g. public/x_spider,public/exa"
    )
    ex.add_argument("--out", required=True, help="bundle path, e.g. tonight.rebox.jsonl.gz")
    ex.add_argument("--instance", help="name of this instance in the bundle header (default: database name)")
    ex.set_defaults(func=cmd_export)

    im = sub.add_parser(
        "import", help="merge a bundle into this instance through the resolver (docs/MERGE.md)"
    )
    im.add_argument("file")
    im.add_argument("--dry-run", action="store_true", help="read only: report matches, new claims, conflicts")
    im.add_argument("--layers", help="comma-separated subset of the bundle's layers")
    im.add_argument("--match-website", action="store_true", help="also match entities by website (weak key)")
    im.add_argument(
        "--batch", type=int, default=exchange.BATCH_CLAIMS, help="bundle claims per resolver batch"
    )
    im.add_argument("--examples", type=int, default=10, help="examples per section of the report")
    im.add_argument("--json", action="store_true", help="print the report as JSON")
    im.set_defaults(func=cmd_import)

    add_dedupe_parser(sub)  # rebox-worker dedupe [--dry-run] (S15)
    rawstore.add_parser(sub)  # rebox-worker raw reindex SOURCE [--dry-run] (S16)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        return int(args.func(args, settings) or 0)
    except (ConfigError, UnknownConnector, exchange.ExchangeError) as e:
        print(f"rebox-worker: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
