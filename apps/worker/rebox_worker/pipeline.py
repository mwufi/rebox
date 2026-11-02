"""The pipeline: fetch -> raw store -> extract -> resolve -> claims writer.

``run_sync``   load cursor, fetch, store new/changed raw records, and every time the connector
               yields a ``CursorUpdate`` (or enough records are pending) flush: extract,
               resolve, write, log resolutions, index the raw records, save the cursor.
``run_replay`` re-extract every stored raw record of a source (no fetch, cursor untouched).
``replay_retract_stale`` a replay, then retract the source's claims the replay no longer emits
               (``rebox-worker replay <source> --retract-stale``; see ``StaleReport``).

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

import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .client import ClaimsClient, minted_ids
from .connectors.base import Connector, Ctx
from .db import Db
from .identity import Active, RunTracker, WorkerSpec, connector_run_stats, run_kind
from .models import ClaimInput, ClaimWrite, CursorUpdate, IdRef, RawRecord
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


def run_replay(
    connector: Connector,
    deps: Deps,
    *,
    batch_records: int = FLUSH_RECORDS,
    emitted: set[str] | None = None,
    after: Callable[[RunTracker, Stats], None] | None = None,
) -> Stats:
    """``emitted`` collects the source_refs extract produced; ``after`` runs inside the replay's
    runs (still open) once every record is written."""
    stats = Stats()
    with _source_lock(deps, connector.name), _recording(deps.db, connector, stats, kind="replay"):
        ctx = replace(deps.ctx, db=deps.ctx.db or deps.db, usage=stats.usage)
        resolver = Resolver(deps.db, source=connector.name, layer=connector.layer)
        with _runs(deps, connector, ctx, "replay", stats) as runs:
            for chunk in _chunked(deps.raw.iter(connector.name), batch_records):
                stats.fetched += len(chunk)
                _write_records(connector, deps, resolver, chunk, stats, runs, emitted=emitted)
                runs.progress(stats)
            if after is not None:
                after(runs, stats)
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
    *,
    emitted: set[str] | None = None,
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
        if emitted is not None:
            emitted.update(c.source_ref for c in claims)
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


# ── replay --retract-stale ────────────────────────────────────────────────

STALE_MAX_SHARE = 0.3  # refuse to retract more than this share of a layer's claims (unless forced)


@dataclass
class RetractStale:
    """Options for ``replay_retract_stale``."""

    dry_run: bool = False
    force: bool = False
    max_share: float = STALE_MAX_SHARE
    examples: int = 10


@dataclass
class StaleReport:
    """What ``replay_retract_stale`` found and did.

    Stale: a non-retracted claim in one of the source's layers, written by one of the source's
    (internal) workers, whose ``source_ref`` the current extract no longer emits for any stored
    raw record. Never stale: claims of other workers in the same layer (``x_parse`` and
    ``x_parse_posts`` share ``public/x_ai``), claims an ``import`` wrote (their raw records are on
    another instance), the name claims the resolver adds for entities it mints
    (``<source>:entity:<id>:name``: emitted only when the entity is first minted), and a ``name``
    claim that is the last live name of an entity that still has other live facts (counted as
    ``kept_names``; retracting it would leave the entity nameless).
    """

    source: str
    dry_run: bool
    workers: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    records: int = 0
    extract_errors: int = 0
    emitted: int = 0  # distinct source_refs the extract emits
    considered: dict[str, int] = field(default_factory=dict)  # layer -> claims checked
    stale: dict[str, int] = field(default_factory=dict)  # layer -> stale claims
    by_predicate: Counter = field(default_factory=Counter)
    by_worker: Counter = field(default_factory=Counter)
    examples: list[dict[str, Any]] = field(default_factory=list)
    kept_names: int = 0  # stale name claims kept: an entity's last name while it has other facts
    refused: str | None = None
    retracted: int = 0
    rejected: int = 0
    runs: dict[str, int] = field(default_factory=dict)  # worker -> run that wrote the retractions
    replay: dict[str, Any] | None = None  # the replay's stats (not in a dry run)

    @property
    def total_stale(self) -> int:
        return sum(self.stale.values())

    def as_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["by_predicate"] = dict(self.by_predicate.most_common())
        out["by_worker"] = dict(self.by_worker.most_common())
        return out

    def render(self) -> str:
        head = f"retract-stale {self.source}" + ("  (DRY RUN: nothing written)" if self.dry_run else "")
        lines = [
            head,
            f"  raw records {self.records}, extract errors {self.extract_errors}, "
            f"source_refs emitted {self.emitted}",
            f"  workers: {', '.join(self.workers) or '(none)'}",
        ]
        for layer in self.layers:
            n, k = self.considered.get(layer, 0), self.stale.get(layer, 0)
            share = f" ({k / n:.1%})" if n else ""
            lines.append(f"  {layer}: {n} claims of these workers, stale {k}{share}")
        if self.kept_names:
            lines.append(
                f"  kept {self.kept_names} stale name claims: the last name of an entity that still has facts"
            )
        if self.by_predicate:
            lines.append(
                "  stale by predicate: " + ", ".join(f"{p} {n}" for p, n in self.by_predicate.most_common())
            )
        if len(self.by_worker) > 1:
            per_worker = ", ".join(f"{w} {n}" for w, n in self.by_worker.most_common())
            lines.append(f"  stale by worker: {per_worker}")
        if self.examples:
            lines.append(f"  examples (first {len(self.examples)}):")
            for e in self.examples:
                what = e["object_id"] or _short(e["value"])
                lines.append(f"    {e['predicate']:<10} {e['subject']} {what}  [{e['source_ref']}]")
        if self.refused:
            lines.append(f"  REFUSED, nothing retracted: {self.refused}")
        elif not self.dry_run:
            runs = ", ".join(f"{w} run {r}" for w, r in sorted(self.runs.items()))
            done = f"  retracted {self.retracted}, rejected {self.rejected}"
            lines.append(done + (f" ({runs})" if runs else ""))
        return "\n".join(lines)


def replay_retract_stale(connector: Connector, deps: Deps, opts: RetractStale | None = None) -> StaleReport:
    """Replay ``connector`` (unless ``opts.dry_run``), then retract its stale claims (see
    ``StaleReport``) as the worker that wrote each, in a ``replay`` run of that worker (the
    replay's own run when it is the same worker). Refuses (writes no retractions; the replay
    still happened) when a layer would lose more than ``max_share`` of its checked claims or an
    extract failed, unless ``opts.force``. Idempotent: a second run finds nothing stale."""
    opts = opts or RetractStale()
    report = StaleReport(source=connector.name, dry_run=opts.dry_run)
    emitted: set[str] = set()
    if opts.dry_run:
        for rec in deps.raw.iter(connector.name):
            report.records += 1
            try:
                emitted.update(c.source_ref for c in connector.extract(rec))
            except Exception:
                report.extract_errors += 1
                log.exception("%s: extract failed for %s", connector.name, rec.external_id)
        _stale_step(connector, deps, opts, report, emitted, None)
        return report

    def after(runs: RunTracker, stats: Stats) -> None:
        report.records, report.extract_errors = stats.fetched, stats.extract_errors
        _stale_step(connector, deps, opts, report, emitted, runs)

    stats = run_replay(connector, deps, emitted=emitted, after=after)
    report.replay = stats.as_dict()
    return report


_STALE_IDS_SQL = """
select c.id, c.layer_id, c.predicate, c.source_ref, c.worker_id
  from claim c left join run r on r.id = c.run_id
 where c.layer_id = any(%(layers)s) and c.worker_id = any(%(workers)s) and not c.retract
   and not coalesce(r.stats ? 'import', false)
   and not (c.predicate = 'name' and c.source_ref like %(resolver_names)s)
"""

_STALE_ROWS_SQL = """
select c.id, c.layer_id, c.subject, c.predicate, c.object_id, c.value, c.valid_from, c.valid_to,
       c.source_ref, c.worker_id, c.evidence, w.kind, w.source
  from claim c join worker w on w.id = c.worker_id
 where c.id = any(%s)
 order by c.layer_id, c.worker_id, c.predicate, c.source_ref, c.id
"""


_EXAMPLE_KEYS = ("layer_id", "worker_id", "subject", "predicate", "object_id", "value", "source_ref")


def _stale_step(
    connector: Connector,
    deps: Deps,
    opts: RetractStale,
    report: StaleReport,
    emitted: set[str],
    runs: RunTracker | None,
) -> None:
    report.emitted = len(emitted)
    with deps.db.view() as conn:
        workers = conn.execute(
            "select id, layers from worker where source = %s and owner = 'rebox' order by id",
            (connector.name,),
        ).fetchall()
        report.workers = [w["id"] for w in workers]
        report.layers = sorted({connector.layer, *(x for w in workers for x in w["layers"])})
        report.considered = dict.fromkeys(report.layers, 0)
        report.stale = dict.fromkeys(report.layers, 0)
        stale_ids: list[int] = []
        if workers:
            like = connector.name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params = {
                "layers": report.layers,
                "workers": report.workers,
                "resolver_names": f"{like}:entity:%:name",
            }
            for r in conn.execute(_STALE_IDS_SQL, params):
                report.considered[r["layer_id"]] += 1
                if r["source_ref"] not in emitted:
                    stale_ids.append(r["id"])
        rows = conn.execute(_STALE_ROWS_SQL, (stale_ids,)).fetchall() if stale_ids else []
        keep = _last_names(conn, rows, stale_ids)
    report.kept_names = len(keep)
    rows = [r for r in rows if r["id"] not in keep]
    for r in rows:
        report.stale[r["layer_id"]] += 1
        report.by_predicate[r["predicate"]] += 1
        report.by_worker[r["worker_id"]] += 1
    report.examples = [{k: _jsonable(r[k]) for k in _EXAMPLE_KEYS} for r in rows[: opts.examples]]
    for layer in report.layers:
        n, k = report.considered[layer], report.stale[layer]
        if k and k > opts.max_share * n and not opts.force:
            report.refused = (
                f"{layer}: {k} of {n} claims ({k / n:.0%}) would be retracted, more than "
                f"{opts.max_share:.0%}; check the raw store (`rebox-worker raw reindex {connector.name}`) "
                "or pass --force"
            )
            break
    if report.refused is None and report.extract_errors and report.total_stale and not opts.force:
        report.refused = (
            f"{report.extract_errors} raw records failed to extract, so their claims would look "
            "stale; fix the extract or pass --force"
        )
    if opts.dry_run or report.refused or not rows:
        return
    assert runs is not None
    groups: dict[tuple[str, str], list[ClaimWrite]] = {}
    kinds: dict[str, tuple[str, str]] = {}
    for r in rows:
        try:
            claim = _retraction(r)
        except ValueError as e:  # e.g. stored evidence the model no longer accepts
            report.rejected += 1
            log.warning("cannot retract claim %s (%s): %s", r["id"], r["source_ref"], e)
            continue
        groups.setdefault((r["worker_id"], r["layer_id"]), []).append(claim)
        kinds[r["worker_id"]] = (r["kind"], r["source"])
    for (worker_id, layer), claims in groups.items():
        active = _active_for(runs, worker_id, *kinds[worker_id])
        result = deps.client.write(layer, claims, worker=worker_id, run_id=active.run_id, drop_rejected=True)
        report.retracted += result.written
        report.rejected += len(result.rejected)
        report.runs[worker_id] = active.run_id
        u = active.stats.usage
        u["retracted_stale"] = u.get("retracted_stale", 0) + result.written
        if result.rejected:
            u["retract_rejected"] = u.get("retract_rejected", 0) + len(result.rejected)
        log.info("%s: %s retracted %d stale claims in %s", connector.name, worker_id, result.written, layer)


_LAST_NAMES_SQL = """
select s.subject,
       exists (select 1 from claim o where o.subject = s.subject and o.predicate = 'name' and not o.retract
                 and o.id <> all(%(stale)s)) as other_name,
       exists (select 1 from claim o where o.subject = s.subject and o.predicate <> 'name' and not o.retract
                 and o.id <> all(%(stale)s))
       or exists (select 1 from claim o where o.object_id = s.subject and not o.retract
                 and o.id <> all(%(stale)s)) as other_facts
  from unnest(%(subjects)s::text[]) as s(subject)
"""


def _last_names(conn: Any, rows: list[dict[str, Any]], stale_ids: list[int]) -> set[int]:
    """Stale ``name`` claims to keep: the last live name of an entity that keeps other live facts
    (e.g. a company an on-topic article references by id, named only by a dropped listicle).
    Retracting it would leave a nameless entity."""
    names = [r for r in rows if r["predicate"] == "name" and r["object_id"] is None]
    if not names:
        return set()
    subjects = sorted({r["subject"] for r in names})
    hits = conn.execute(_LAST_NAMES_SQL, {"subjects": subjects, "stale": stale_ids}).fetchall()
    keep_subjects = {h["subject"] for h in hits if not h["other_name"] and h["other_facts"]}
    return {r["id"] for r in names if r["subject"] in keep_subjects}


def _retraction(r: dict[str, Any]) -> ClaimWrite:
    """The stored claim, written again with ``retract: true`` (same upsert key, CONTRACTS §5)."""
    try:
        return ClaimWrite(
            subject=IdRef(id=r["subject"]),
            predicate=r["predicate"],
            object=IdRef(id=r["object_id"]) if r["object_id"] else None,
            value=r["value"],
            valid_from=_iso(r["valid_from"]),
            valid_to=_iso(r["valid_to"]),
            retract=True,
            source_ref=r["source_ref"],
            evidence=r["evidence"] or None,
        )
    except Exception as e:  # pydantic.ValidationError
        raise ValueError(str(e).splitlines()[0]) from e


def _active_for(runs: RunTracker, worker_id: str, kind: str, source: str) -> Active:
    """The run ``worker_id`` writes in: the replay's own if this worker wrote there, else a new
    ``replay`` run (closed with the replay's)."""
    for a in runs.active.values():
        if a.worker_id == worker_id:
            return a
    m = re.match(rf"^(?P<name>.+)-{re.escape(kind)}-\d{{6}}$", worker_id)
    spec = WorkerSpec(name=m.group("name") if m else worker_id, kind=kind, source=source)
    run_id = runs.client.open_run(worker_id, run_kind(spec, "replay"))
    a = Active(spec, worker_id, run_id, runs.new_stats())
    runs.active[(f"#{worker_id}", kind, source)] = a
    return a


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else v


def _jsonable(v: Any) -> Any:
    return _iso(v) if isinstance(v, datetime) else v


def _short(v: Any, limit: int = 60) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"
