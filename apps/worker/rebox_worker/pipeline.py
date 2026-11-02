"""The pipeline: fetch -> raw store -> extract -> resolve -> claims writer.

``run_sync``   load cursor, fetch, store new/changed raw records, and every time the connector
               yields a ``CursorUpdate`` (or enough records are pending) flush: extract,
               resolve, write, log resolutions, index the raw records, save the cursor.
``run_replay`` re-extract every stored raw record of a source (no fetch, cursor untouched).

A flush is all-or-nothing from the pipeline's point of view: the raw index and the cursor
only move after the writer accepted every claim, and minted ids are deterministic, so a
crash or retry repeats work but never loses or duplicates it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .client import ClaimsClient
from .connectors.base import Connector, Ctx
from .db import Db
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

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


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
        resolver = Resolver(deps.db, source=connector.name, layer=connector.layer)
        cursor = load_cursor(deps.db, connector)
        pending = _Pending()
        for item in connector.fetch(deps.ctx, cursor):
            if isinstance(item, CursorUpdate):
                pending.cursor = item.cursor
                _flush(connector, deps, resolver, pending, stats)
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
                _flush(connector, deps, resolver, pending, stats)
        _flush(connector, deps, resolver, pending, stats)
    return stats


def run_replay(connector: Connector, deps: Deps, *, batch_records: int = FLUSH_RECORDS) -> Stats:
    stats = Stats()
    with _source_lock(deps, connector.name), _recording(deps.db, connector, stats, kind="replay"):
        resolver = Resolver(deps.db, source=connector.name, layer=connector.layer)
        for chunk in _chunked(deps.raw.iter(connector.name), batch_records):
            stats.fetched += len(chunk)
            _write_records(connector, deps, resolver, chunk, stats)
    return stats


# ── steps ─────────────────────────────────────────────────────────────────


def _flush(connector: Connector, deps: Deps, resolver: Resolver, pending: _Pending, stats: Stats) -> None:
    if pending.records:
        _write_records(connector, deps, resolver, [r for r, _ in pending.records], stats)
        for rec, key in pending.records:  # only now are these records "seen"
            deps.raw.index(rec, key)
        pending.records.clear()
    if pending.cursor is not None:
        save_cursor(deps.db, connector.name, pending.cursor)
        pending.cursor = None


def _write_records(
    connector: Connector, deps: Deps, resolver: Resolver, records: list[RawRecord], stats: Stats
) -> None:
    items: list[tuple[str, ClaimInput]] = []
    for rec in records:
        try:
            items.extend((rec.external_id, c) for c in connector.extract(rec))
        except Exception:
            stats.extract_errors += 1
            log.exception("%s: extract failed for %s", connector.name, rec.external_id)
    if not items:
        return
    res = resolver.resolve(items)
    result = deps.client.write(connector.layer, res.claims)
    resolver.commit(res)  # resolution_log + review items, after the writer accepted the claims
    stats.batches += 1
    stats.claims += len(res.claims)
    stats.written += result.written
    stats.minted += res.count("minted")
    stats.matched += res.count("matched")
    stats.review += res.count("review")
    stats.rejected += len(res.rejected)
    log.info(
        "%s: wrote %d claims (%d minted, %d matched, %d review, %d rejected)",
        connector.name,
        result.written,
        res.count("minted"),
        res.count("matched"),
        res.count("review"),
        len(res.rejected),
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
