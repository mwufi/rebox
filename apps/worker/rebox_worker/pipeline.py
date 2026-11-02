"""The pipeline: fetch -> raw store -> extract -> resolve -> claims writer.

``run_sync``   load cursor, fetch, store new/changed raw records, and every time the connector
               yields a ``CursorUpdate`` (or enough records are pending) flush: extract,
               resolve, write, log resolutions, index the raw records, save the cursor.
``run_replay`` re-extract every stored raw record of a source (no fetch, cursor untouched).

A flush is all-or-nothing from the pipeline's point of view: the raw index and the cursor
only move after the flush's writes went through, and minted ids are deterministic, so a
crash or retry repeats work but never loses or duplicates it.

Rejected claims never stop a run: the resolver drops claims that break the writer's rules
before sending, and the client drops claims the writer still refuses (by index in a 400) and
their dependents, then writes the rest (``drop_rejected``). Both count as ``rejected`` in the
run stats and are logged with their reasons. Their records are indexed like the others (a
``replay`` after a fix re-extracts them). Only transport/5xx errors (after retries), auth
errors and 400s that are not about specific claims fail the run.

Provenance (CONTRACTS §12): each sync/replay resolves the connector's worker identity, opens a
run, sends ``worker`` + ``run_id`` with every batch, reports progress after each flush and
closes the run with its final stats (``failed`` with the error if anything raised). See
``identity.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .client import ClaimsClient, minted_ids
from .connectors.base import Connector, Ctx
from .db import Db
from .identity import Active, RunTracker, connector_run_stats
from .models import ClaimInput, CursorUpdate, RawRecord
from .rawstore import RawStore
from .resolve import Resolver

log = logging.getLogger(__name__)

FLUSH_RECORDS = 200  # flush at least this often even if the connector never yields a CursorUpdate


class SourceBusy(RuntimeError):
    """Another worker is syncing or replaying this source right now."""


@dataclass
class Stats:
    fetched: int = 0
    skipped_unchanged: int = 0
    extract_errors: int = 0
    claims: int = 0
    written: int = 0
    minted: int = 0
    matched: int = 0
    review: int = 0
    rejected: int = 0
    batches: int = 0
    # Usage reported by the connector (ctx.add_usage, connector.run_stats): api_calls, cost_usd...
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "usage"}
        out.update(self.usage)
        return out

    def add(self, other: Stats) -> None:
        for f in fields(self):
            if f.name != "usage":
                setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))


@dataclass
class Deps:
    """Everything a run needs; built once by the CLI / job runner."""

    db: Db
    raw: RawStore
    client: ClaimsClient
    ctx: Ctx = field(default_factory=Ctx)
    lock_url: str | None = None  # DSN for the per-source advisory lock (None = no lock)


@dataclass
class _Pending:
    records: list[tuple[RawRecord, str]] = field(default_factory=list)  # (record, store key)
    cursor: dict[str, Any] | None = None


# ── entry points ──────────────────────────────────────────────────────────


def run_sync(connector: Connector, deps: Deps) -> Stats:
    stats = Stats()
    with _source_lock(deps, connector.name), _recording(deps.db, connector, stats):
        # Connectors that read the graph (X) use the worker's pool, never a connection of their own;
        # usage counters are per run.
        ctx = replace(deps.ctx, db=deps.ctx.db or deps.db, usage=stats.usage)
        resolver = Resolver(deps.db, source=connector.name, layer=connector.layer)
        cursor = load_cursor(deps.db, connector)
        with _runs(deps, connector, ctx, "sync", stats) as runs:
            pending = _Pending()
            for item in connector.fetch(ctx, cursor):
                if isinstance(item, CursorUpdate):
                    pending.cursor = item.cursor
                    _flush(connector, deps, resolver, pending, stats, runs)
                    continue
                if item.source != connector.name:  # replay iterates by connector.name; never lose records
                    raise ValueError(f"{connector.name}: fetch yielded a record for source {item.source!r}")
                stats.fetched += 1
                put = deps.raw.put(item, index=False)
                if put.skipped:
                    stats.skipped_unchanged += 1
                    continue
                pending.records.append((item, put.key))
                if len(pending.records) >= FLUSH_RECORDS:
                    _flush(connector, deps, resolver, pending, stats, runs)
            _flush(connector, deps, resolver, pending, stats, runs)
    return stats


def run_replay(connector: Connector, deps: Deps, *, batch_records: int = FLUSH_RECORDS) -> Stats:
    stats = Stats()
    with _source_lock(deps, connector.name), _recording(deps.db, connector, stats, kind="replay"):
        ctx = replace(deps.ctx, db=deps.ctx.db or deps.db, usage=stats.usage)
        resolver = Resolver(deps.db, source=connector.name, layer=connector.layer)
        with _runs(deps, connector, ctx, "replay", stats) as runs:
            for chunk in _chunked(deps.raw.iter(connector.name), batch_records):
                stats.fetched += len(chunk)
                _write_records(connector, deps, resolver, chunk, stats, runs)
                runs.progress(stats)
    return stats


@contextmanager
def _runs(deps: Deps, connector: Connector, ctx: Ctx, kind: str, stats: Stats) -> Iterator[RunTracker]:
    """Open the run(s) for this sync/replay and close them with final stats, ok or failed."""
    tracker = RunTracker(deps.client, connector, ctx, kind, Stats)
    try:
        yield tracker
    except BaseException as e:
        stats.usage.update(connector_run_stats(connector))
        tracker.close(stats, e)
        raise
    # The connector's own run_stats (api_calls, cost_usd, x_stop, ...) also land in the returned
    # Stats, so the CLI output and connector_state.last_stats show them.
    stats.usage.update(connector_run_stats(connector))
    tracker.close(stats, None)


# ── steps ─────────────────────────────────────────────────────────────────


def _flush(
    connector: Connector, deps: Deps, resolver: Resolver, pending: _Pending, stats: Stats, runs: RunTracker
) -> None:
    if pending.records:
        _write_records(connector, deps, resolver, [r for r, _ in pending.records], stats, runs)
        for rec, key in pending.records:  # only now are these records "seen"
            deps.raw.index(rec, key)
        pending.records.clear()
        runs.progress(stats)
    if pending.cursor is not None:
        save_cursor(deps.db, connector.name, pending.cursor)
        pending.cursor = None


def _write_records(
    connector: Connector,
    deps: Deps,
    resolver: Resolver,
    records: list[RawRecord],
    stats: Stats,
    runs: RunTracker,
) -> None:
    # Records are grouped by the worker identity that writes them (one, unless the connector
    # defines worker_for); each group is resolved and written as that worker, in its run.
    groups: dict[int, tuple[Active, list[tuple[str, ClaimInput]]]] = {}
    for rec in records:
        try:
            claims = connector.extract(rec)
        except Exception:
            stats.extract_errors += 1
            log.exception("%s: extract failed for %s", connector.name, rec.external_id)
            continue
        if claims:
            active = runs.for_record(rec)
            groups.setdefault(id(active), (active, []))[1].extend((rec.external_id, c) for c in claims)
    for active, items in groups.values():
        batch = Stats()
        res = resolver.resolve(items)
        result = deps.client.write(
            connector.layer, res.claims, worker=active.worker_id, run_id=active.run_id, drop_rejected=True
        )
        resolver.commit(res)  # resolution_log + review items, after the writer accepted the claims
        batch.batches = 1
        batch.claims = len(res.claims)
        batch.written = result.written
        batch.minted = res.count("minted")
        if result.rejected:  # an entity whose every claim was refused was not minted after all
            refused = {id(r.claim) for r in result.rejected}
            kept = set().union(*(minted_ids(c) for c in res.claims if id(c) not in refused))
            batch.minted = sum(1 for r in res.logs if r.decision == "minted" and r.entity_id in kept)
        batch.matched = res.count("matched")
        batch.review = res.count("review")
        # Refused before sending (resolver) + refused by the writer (and their dependents).
        batch.rejected = len(res.rejected) + len(result.rejected)
        stats.add(batch)
        active.stats.add(batch)
        log.info(
            "%s: %s wrote %d claims (%d minted, %d matched, %d review, %d rejected)",
            connector.name,
            active.worker_id,
            result.written,
            batch.minted,
            batch.matched,
            batch.review,
            batch.rejected,
        )


# ── connector_state ───────────────────────────────────────────────────────


def ensure_state(conn: psycopg.Connection, connector: Connector) -> None:
    """Create the connector's state row on first sight (interval from the connector).

    An existing row is left alone: ``interval_s`` and ``next_run_at`` are operational
    settings that may be tuned in the database.
    """
    conn.execute(
        "insert into connector_state (source, interval_s) values (%s, %s) on conflict (source) do nothing",
        (connector.name, connector.interval_s),
    )


def load_cursor(db: Db, connector: Connector) -> dict[str, Any] | None:
    with db.view() as conn:
        ensure_state(conn, connector)
        row = conn.execute(
            "select cursor from connector_state where source = %s", (connector.name,)
        ).fetchone()
    return row["cursor"] if row else None


def save_cursor(db: Db, source: str, cursor: dict[str, Any]) -> None:
    with db.view() as conn:
        conn.execute("update connector_state set cursor = %s where source = %s", (Jsonb(cursor), source))


@contextmanager
def _recording(db: Db, connector: Connector, stats: Stats, kind: str = "sync") -> Iterator[None]:
    """Save last_run_at / last_status / last_stats when the run ends (ok or error)."""
    with db.view() as conn:
        ensure_state(conn, connector)
    status = "ok"
    try:
        yield
    except BaseException as e:
        status = f"error: {type(e).__name__}: {e}"[:500]
        raise
    finally:
        with db.view() as conn:
            conn.execute(
                """update connector_state
                      set last_run_at = now(), last_status = %s, last_stats = %s
                    where source = %s""",
                (status if kind == "sync" else f"{kind} {status}", Jsonb(stats.as_dict()), connector.name),
            )


@contextmanager
def _source_lock(deps: Deps, source: str) -> Iterator[None]:
    """A session advisory lock per source, so two workers never sync/replay one source at once."""
    if deps.lock_url is None:
        yield
        return
    with psycopg.connect(deps.lock_url, autocommit=True) as conn:
        key = f"rebox-worker:source:{source}"
        got = conn.execute("select pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()
        if not got or not got[0]:
            raise SourceBusy(f"source {source!r} is already being processed by another worker")
        try:
            yield
        finally:
            conn.execute("select pg_advisory_unlock(hashtext(%s))", (key,))


def _chunked(it: Iterable[RawRecord], n: int) -> Iterator[list[RawRecord]]:
    chunk: list[RawRecord] = []
    for x in it:
        chunk.append(x)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
