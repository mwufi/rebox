"""Postgres job queue (table ``job``) and the scheduler.

Lifecycle: ``queued`` -> (claim) ``running`` -> ``done``, or back to ``queued`` with
``run_after = now() + backoff`` until ``attempts = max_attempts``, then ``failed``.

``claim`` uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers never take the same job.
A job whose worker died stays ``running``; ``requeue_stale`` returns it to the queue after
``stale_after_s`` (run by ``rebox-worker run`` at startup and between jobs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from .db import Db

log = logging.getLogger(__name__)

JOB_KINDS = ("sync", "replay")
BACKOFF_BASE_S = 60
BACKOFF_MAX_S = 3600
STALE_AFTER_S = 6 * 3600


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int  # including the current attempt
    max_attempts: int

    @property
    def source(self) -> str:
        """The payload's source ('' if missing: the runner then fails the job as unknown)."""
        return str(self.payload.get("source") or "")


def backoff_s(attempts: int, base_s: int = BACKOFF_BASE_S, max_s: int = BACKOFF_MAX_S) -> int:
    """Delay before retry number ``attempts`` (1-based): base, 2*base, 4*base, ... capped."""
    return min(max_s, base_s * 2 ** max(0, attempts - 1))


def enqueue(
    db: Db,
    kind: str,
    source: str,
    *,
    max_attempts: int = 3,
    run_after: datetime | None = None,
) -> int:
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind {kind!r} (expected one of {JOB_KINDS})")
    with db.view() as conn:
        row = conn.execute(
            """insert into job (kind, payload, max_attempts, run_after)
               values (%s, %s, %s, coalesce(%s, now())) returning id""",
            (kind, Jsonb({"source": source}), max_attempts, run_after),
        ).fetchone()
    assert row is not None
    return int(row["id"])


def claim(db: Db) -> Job | None:
    """Take the oldest ready job, mark it running and count the attempt. None if the queue is empty."""
    with db.view() as conn:
        row = conn.execute(
            """update job set status = 'running', attempts = attempts + 1, locked_at = now()
                where id = (select id from job
                             where status = 'queued' and run_after <= now()
                             order by run_after, id
                             limit 1
                             for update skip locked)
            returning id, kind, payload, attempts, max_attempts"""
        ).fetchone()
    if row is None:
        return None
    return Job(row["id"], row["kind"], dict(row["payload"] or {}), row["attempts"], row["max_attempts"])


def mark_done(db: Db, job: Job) -> None:
    with db.view() as conn:
        conn.execute(
            """update job set status = 'done', finished_at = now(), locked_at = null, last_error = null
                where id = %s""",
            (job.id,),
        )


def mark_failed(db: Db, job: Job, error: str, *, base_s: int = BACKOFF_BASE_S, retry: bool = True) -> str:
    """Record a failure: re-queue with exponential backoff, or fail for good. Returns the new status."""
    final = not retry or job.attempts >= job.max_attempts
    with db.view() as conn:
        if final:
            conn.execute(
                """update job set status = 'failed', finished_at = now(), locked_at = null, last_error = %s
                    where id = %s""",
                (error[:4000], job.id),
            )
            return "failed"
        conn.execute(
            """update job set status = 'queued', locked_at = null, last_error = %s,
                              run_after = now() + make_interval(secs => %s)
                where id = %s""",
            (error[:4000], backoff_s(job.attempts, base_s), job.id),
        )
    return "queued"


def requeue_stale(db: Db, stale_after_s: int = STALE_AFTER_S) -> int:
    """Running jobs whose worker vanished go back to the queue (their attempt still counts)."""
    with db.view() as conn:
        cur = conn.execute(
            """update job set status = case when attempts >= max_attempts then 'failed' else 'queued' end,
                              locked_at = null, run_after = now(),
                              last_error = coalesce(last_error, '') || ' [stale: worker lost]',
                              finished_at = case when attempts >= max_attempts then now() end
                where status = 'running' and locked_at < now() - make_interval(secs => %s)""",
            (stale_after_s,),
        )
        return cur.rowcount


def schedule_due(db: Db, connectors: dict[str, Any] | None = None, ctx: Any = None) -> list[str]:
    """Enqueue a ``sync`` for every connector that is due, and advance its ``next_run_at``.

    Due = ``next_run_at <= now()`` and no queued/running sync for that source. Idempotent:
    running it twice in a row enqueues nothing the second time.

    ``connectors`` (name -> connector instance) restricts scheduling to the connectors that are
    enabled (``enabled(ctx)``, e.g. a token is set in ``ctx.env``) and scheduled (not
    ``scheduled = False``, like ``example``), and registers a state row with
    ``next_run_at = now()`` for any of them that has none, so a fresh stack syncs right away.
    ``ctx`` is the connector context (the CLI passes ``Ctx(env=os.environ)``).
    """
    from .connectors.base import Ctx, is_enabled, is_scheduled  # local: avoid a cycle at import time
    from .pipeline import ensure_state

    eligible: set[str] | None = None
    with db.view() as conn:
        if connectors is not None:
            ctx = ctx if ctx is not None else Ctx()
            eligible = {name for name, c in connectors.items() if is_scheduled(c) and is_enabled(c, ctx)}
            for name in sorted(eligible):
                ensure_state(conn, connectors[name])
        # Lock the due rows so two schedulers cannot both enqueue the same source.
        rows = conn.execute(
            """select source, interval_s from connector_state
                where next_run_at <= now()
                order by source
                for update skip locked"""
        ).fetchall()
        enqueued: list[str] = []
        for row in rows:
            source = row["source"]
            if eligible is not None and source not in eligible:
                continue
            active = conn.execute(
                """select 1 from job where kind = 'sync' and status in ('queued','running')
                     and payload->>'source' = %s limit 1""",
                (source,),
            ).fetchone()
            if active is None:
                conn.execute(
                    "insert into job (kind, payload) values ('sync', %s)", (Jsonb({"source": source}),)
                )
                enqueued.append(source)
            # Advance from now (not from the old next_run_at) so a long outage yields one run, not many.
            conn.execute(
                """update connector_state set next_run_at = now() + make_interval(secs => interval_s)
                    where source = %s""",
                (source,),
            )
    return enqueued
