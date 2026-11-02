"""``rebox-worker`` command line.

rebox-worker run [--once]          claim and run queued jobs (loop)
rebox-worker sync <source>         run a connector now, inline
rebox-worker replay <source>       re-extract a source from the raw store
rebox-worker schedule-due          enqueue syncs for connectors that are due
rebox-worker enqueue <source>      queue a sync (or --kind replay)
rebox-worker status                connector_state table
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

from . import __version__, jobs
from .client import ClaimsClient
from .config import ConfigError, Settings
from .connectors import REGISTRY, Ctx, UnknownConnector, get_connector
from .db import Db
from .pipeline import Deps, SourceBusy, Stats, run_replay, run_sync
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
    with open_deps(settings) as deps:
        stats = run_replay(connector, deps)
    print(f"replay {args.source}: " + json.dumps(stats.as_dict()))
    return 0


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
    except (ConfigError, UnknownConnector) as e:
        print(f"rebox-worker: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
