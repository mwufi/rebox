"""Moving whole layers between Rebox instances: export -> bundle -> import through the resolver (S11).

Why not copy ids
    Ids are deterministic per strongest identity key, but the same person can be minted from
    different keys on different instances (``github`` on one machine, ``x_handle`` on another).
    So the import never trusts the source's ids: every entity goes through the **target's
    resolver** (hard keys -> scoped fuzzy -> mint), exactly like a connector's output.

Bundle (``.rebox.jsonl.gz``), one JSON object per line, deterministic order::

    {"type": "header", "version": 1, "source_instance", "exported_at", "layers": [...],
     "workers": [{id, kind, source, owner, trust, layers, config, description}],
     "claims": {layer: n}, "entities": n}
    {"type": "claim", "layer", "worker", "subject": Ref, "predicate", "object": Ref | null,
     "value", "valid_from", "valid_to", "retract", "source_ref", "evidence"}

    Ref = {"kind", "identity": {<identity predicate>: value | [values]}, "name", "origin_id"}

``identity`` holds every identity value the source knows for the entity (and for entities
merged into it): its identity claims in all visible public layers, plus the keys the source's
resolver actually matched or minted it by (``resolution_log``; a parser may mint a company from
an ``x_handle`` it never writes as a claim). They are hints for the target's resolver, not
claims. ``origin_id`` is the source's canonical id, also only a hint.

Import (``import_bundle``)
    1. Plan workers: every worker of the bundle keeps its id on the target (``POST
       /internal/workers`` with an explicit ``id``; idempotent). The same id with a different
       kind/source/owner on the target aborts the import before anything is written. Claims
       without a worker (written before migration 0005) are written as the target's
       ``<source>-connector`` worker.
    2. Stream the claims in batches, grouped by (layer, worker), through ``ImportResolver``,
       then diff them against the target (same layer + source_ref + subject + predicate +
       object): unchanged claims are not sent, so re-importing a bundle writes nothing (no
       claims, no runs, no resolution log). Each worker that writes gets one run of kind
       ``replay`` with its stats. Evidence is preserved.
    3. ``dry_run`` reads only: it reports entities matched vs minted per kind (with examples of
       origin id -> target id and the key that matched), claims per layer (new / changed /
       unchanged / rejected), and conflicts (a ``one`` attribute whose current winning value on
       the target differs from the incoming one, and whether the incoming claim would win).

Hints the import does not use (``ImportOptions``)
    * ``website`` values (by default): a person's website is often their employer's
      (four a16z partners share ``a16z.com``), so it is a weak key. ``match_website=True``
      uses it.
    * Values shared by two or more different origin entities of the same kind in the bundle:
      they do not identify anything on the source, so they must not merge on the target.

``ImportResolver`` adds one rule after the scoped fuzzy pass, before minting: if the origin id
itself exists on the target (same deterministic derivation, e.g. a Wikidata place both sides
minted from its QID), that entity is the match. Entities with no minting key left (identity-less,
or only keys like ``x_id`` that are not in the CONTRACTS §3 order) keep their origin id, so
importing them is idempotent across batches and never collapses two of them that happen to share
a name.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from pydantic import PrivateAttr, ValidationError

from .client import BATCH_SIZE, ClaimsClient, ValidationRejected, chunks
from .db import Db, public_layers, set_view
from .identity import worker_name
from .ids import strongest_key
from .models import ClaimInput, ClaimWrite, EntityRef, Evidence, IdRef
from .predicates import Predicate, load_predicates
from .resolve import (
    AMBIGUOUS_CONFIDENCE,
    LogRow,
    Resolved,
    Resolver,
    ReviewRow,
    _Batch,
    _canonicalize,
    _Decision,
    _Ref,
)

log = logging.getLogger(__name__)

BUNDLE_VERSION = 1
BATCH_CLAIMS = 500  # bundle claims per resolver batch
WEAK_HINT_KEYS = frozenset({"website"})
DEFAULT_TRUST = {"connector": 0.8, "spider": 0.8, "parser": 0.6, "enricher": 0.5, "external": 0.3}
UNATTRIBUTED_TRUST = 0.5  # fold trust of claims without a worker (CONTRACTS §12)
_WORKER_ID_RE = re.compile(r"^(?P<name>.+)-(?P<kind>connector|spider|parser|enricher|external)-\d{6}$")


class ExchangeError(RuntimeError):
    pass


class ImportAborted(ExchangeError):
    """The import cannot proceed safely (e.g. a worker id conflict). Nothing was written."""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _iso(v: datetime | None) -> str | None:
    """UTC, so a bundle does not depend on the exporting session's time zone."""
    return v.astimezone(UTC).isoformat() if v is not None else None


# ══ export ═════════════════════════════════════════════════════════════════


@dataclass
class ExportSummary:
    path: str
    layers: list[str]
    claims: dict[str, int]
    entities: int
    workers: list[str]
    unattributed: int  # claims without a worker

    def render(self) -> str:
        total = sum(self.claims.values())
        lines = [
            f"exported {total} claims, {self.entities} entities, {len(self.workers)} workers -> {self.path}"
        ]
        lines += [f"  {layer}: {n} claims" for layer, n in sorted(self.claims.items())]
        lines += [f"  worker {w}" for w in self.workers]
        if self.unattributed:
            lines.append(
                f"  {self.unattributed} claims have no worker (pre-0005); they import as <source>-connector"
            )
        return "\n".join(lines)


@contextmanager
def _snapshot(db: Db) -> Iterator[psycopg.Connection]:
    """One repeatable-read transaction with all public layers visible: a consistent export even
    while spiders keep writing."""
    with db.pool.connection() as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        try:
            with conn.transaction():
                set_view(conn, public_layers(conn))
                yield conn
        finally:
            conn.isolation_level = None


def export_layers(
    db: Db, layers: Sequence[str], out: str | os.PathLike[str], *, source_instance: str | None = None
) -> ExportSummary:
    """Write every claim of ``layers`` (not folded) with its worker and evidence to ``out``."""
    layers = sorted(set(layers))
    if not layers:
        raise ExchangeError("no layers to export")
    out = Path(out)
    with _snapshot(db) as conn:
        visible = set(public_layers(conn))
        missing = [layer for layer in layers if layer not in visible]
        if missing:
            raise ExchangeError(f"unknown or non-public layers: {', '.join(missing)}")
        instance = source_instance or conn.execute("select current_database() as db").fetchone()["db"]
        preds = load_predicates(conn)
        identity_preds = sorted(p.id for p in preds.values() if p.is_identity)

        counts = {
            r["layer_id"]: r["n"]
            for r in conn.execute(
                "select layer_id, count(*) as n from claim where layer_id = any(%s) group by layer_id",
                (layers,),
            )
        }
        worker_rows = conn.execute(
            """select w.id, w.kind, w.source, w.owner, w.trust, w.layers, w.config, w.description
                 from worker w
                where w.id in (select distinct worker_id from claim
                                where layer_id = any(%s) and worker_id is not null)
                order by w.id""",
            (layers,),
        ).fetchall()
        unattributed = conn.execute(
            "select count(*) as n from claim where layer_id = any(%s) and worker_id is null", (layers,)
        ).fetchone()["n"]
        refs = _entity_refs(conn, layers, identity_preds)

        header = {
            "type": "header",
            "version": BUNDLE_VERSION,
            "source_instance": instance,
            "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "layers": layers,
            "workers": [
                {
                    "id": w["id"],
                    "kind": w["kind"],
                    "source": w["source"],
                    "owner": w["owner"],
                    "trust": round(float(w["trust"]), 4),
                    "layers": list(w["layers"]),
                    "config": w["config"] or {},
                    "description": w["description"],
                }
                for w in worker_rows
            ],
            "claims": {layer: counts.get(layer, 0) for layer in layers},
            "entities": len({r["origin_id"] for r in refs.values()}),
        }

        tmp = out.with_name(out.name + ".tmp")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            text = io.TextIOWrapper(gz, encoding="utf-8", newline="\n")
            text.write(_dumps(header) + "\n")
            with conn.cursor(name="rebox_export_claims") as cur:
                cur.itersize = 2000
                cur.execute(
                    """select layer_id, worker_id, subject, predicate, object_id, value, valid_from, valid_to,
                              retract, source_ref, evidence
                         from claim where layer_id = any(%s)
                        order by layer_id, worker_id nulls first, subject, predicate,
                                 coalesce(object_id, ''), source_ref, id""",
                    (layers,),
                )
                for c in cur:
                    text.write(
                        _dumps(
                            {
                                "type": "claim",
                                "layer": c["layer_id"],
                                "worker": c["worker_id"],
                                "subject": refs[c["subject"]],
                                "predicate": c["predicate"],
                                "object": refs[c["object_id"]] if c["object_id"] else None,
                                "value": c["value"],
                                "valid_from": _iso(c["valid_from"]),
                                "valid_to": _iso(c["valid_to"]),
                                "retract": bool(c["retract"]),
                                "source_ref": c["source_ref"],
                                "evidence": c["evidence"],
                            }
                        )
                        + "\n"
                    )
            text.flush()
            text.detach()
        os.replace(tmp, out)
    return ExportSummary(
        path=str(out),
        layers=layers,
        claims=header["claims"],
        entities=header["entities"],
        workers=[w["id"] for w in header["workers"]],
        unattributed=unattributed,
    )


def _entity_refs(conn: psycopg.Connection, layers: list[str], identity_preds: list[str]) -> dict[str, dict]:
    """Unresolved refs for every entity the layers' claims mention: id -> {kind, identity, name, origin_id}.

    Merged entities export as their canonical entity, with the identity of the whole merge group.
    """
    ids = sorted(
        r["id"]
        for r in conn.execute(
            """select subject as id from claim where layer_id = any(%s)
               union select object_id from claim where layer_id = any(%s) and object_id is not null""",
            (layers, layers),
        )
    )
    found = _canonicalize(conn, ids)
    canon = {i: c.id for i, c in found.items()}
    kinds = {c.id: c.kind for c in found.values()}
    for i in ids:  # not visible as an entity (should not happen for public layers): keep it as is
        canon.setdefault(i, i)
        kinds.setdefault(canon[i], i.split(":")[1])
    # Merge groups: every entity whose merged_into chain ends at one of ours.
    merged = {
        r["id"]: r["merged_into"]
        for r in conn.execute("select id, merged_into from entity where merged_into is not null")
    }
    members: dict[str, set[str]] = defaultdict(set)
    for c in set(canon.values()):
        members[c].add(c)
    for i in merged:
        end, seen = i, 0
        while end in merged and seen < 50:
            end, seen = merged[end], seen + 1
        if end in members:
            members[end].add(i)
    for i, c in canon.items():
        members[c].add(i)
    owner = {m: c for c, ms in members.items() for m in ms}
    all_ids = sorted(owner)

    identity: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in conn.execute(
        """select subject, predicate, value_text from claim
            where subject = any(%s) and predicate = any(%s) and object_id is null and not retract
              and value_text is not null""",
        (all_ids, identity_preds),
    ):
        identity[owner[r["subject"]]][r["predicate"]].add(r["value_text"])
    # The keys the source's resolver matched or minted the entity by (some are never written as claims).
    for r in conn.execute(
        """select entity_id, kind, identity -> 'identity' as pairs from resolution_log
            where entity_id = any(%s) and decision in ('matched', 'minted')""",
        (all_ids,),
    ):
        c = owner[r["entity_id"]]
        if r["kind"] != kinds.get(c) or not isinstance(r["pairs"], dict):
            continue
        for p, v in r["pairs"].items():
            if p in identity_preds and isinstance(v, str | int) and str(v):
                identity[c][p].add(str(v))

    names: dict[str, str] = {}
    for r in conn.execute(
        """select c.subject, c.value_text from claim c left join worker w on w.id = c.worker_id
            where c.subject = any(%s) and c.predicate = 'name' and c.object_id is null and not c.retract
              and c.value_text is not null
            order by (c.subject = any(%s)) desc, (c.layer_id = any(%s)) desc,
                     coalesce(w.trust, %s) desc, c.updated_at desc, c.id desc""",
        (all_ids, sorted(members), layers, UNATTRIBUTED_TRUST),
    ):
        names.setdefault(owner[r["subject"]], r["value_text"])

    out: dict[str, dict] = {}
    for i, c in canon.items():
        ident = {
            p: (sorted(vs)[0] if len(vs) == 1 else sorted(vs))
            for p, vs in sorted(identity.get(c, {}).items())
            if vs
        }
        out[i] = {"kind": kinds[c], "identity": ident, "name": names.get(c), "origin_id": c}
    return out


# ══ bundle reading ═════════════════════════════════════════════════════════


def _check_header(path: str | os.PathLike[str], line: str) -> dict[str, Any]:
    try:
        header = json.loads(line)
    except ValueError as e:
        raise ExchangeError(f"{path}: not a Rebox bundle ({e})") from None
    if not isinstance(header, dict) or header.get("type") != "header":
        raise ExchangeError(f"{path}: first line is not a bundle header")
    if header.get("version") != BUNDLE_VERSION:
        raise ExchangeError(
            f"{path}: bundle version {header.get('version')!r}, this worker reads {BUNDLE_VERSION}"
        )
    return header


def read_header(path: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return _check_header(path, f.readline())
    except (OSError, EOFError) as e:
        raise ExchangeError(f"{path}: cannot read bundle: {e}") from None


def iter_claims(path: str | os.PathLike[str]) -> Iterator[dict[str, Any]]:
    """The claim lines of a bundle, in file order (the header is checked and skipped)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        _check_header(path, f.readline())
        for n, line in enumerate(f, start=2):
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("type") != "claim":
                raise ExchangeError(f"{path}:{n}: unexpected line type {obj.get('type')!r}")
            yield obj


# ══ import: resolver ═══════════════════════════════════════════════════════


class OriginRef(EntityRef):
    """An EntityRef that remembers the source's id (a hint, never written)."""

    _origin: str | None = PrivateAttr(default=None)

    @classmethod
    def of(cls, kind: str, identity: dict[str, Any], name: str | None, origin: str | None) -> OriginRef:
        ref = cls(kind=kind, identity=identity, name=name)
        ref._origin = origin
        return ref

    @property
    def origin(self) -> str | None:
        return self._origin


class ImportResolver(Resolver):
    """The normal resolver plus the origin-id hint (see the module docstring).

    ``named_origins``: origins the bundle names itself, so minting them adds no generated name
    claim. ``on_decision(origin, ref, log_row)`` is called for every new resolution decision.
    """

    def __init__(
        self,
        db: Db,
        *,
        source: str,
        layer: str,
        predicates: dict[str, Predicate] | None = None,
        named_origins: set[str] | frozenset[str] = frozenset(),
        on_decision: Any = None,
    ) -> None:
        super().__init__(db, source=source, layer=layer, predicates=predicates)
        self.named_origins = named_origins
        self.on_decision = on_decision
        self._origin: str | None = None
        self._origin_hits: dict[str, Any] = {}

    def resolve(self, items):  # type: ignore[override]
        origins: set[str] = set()
        for item in items:
            c = item if isinstance(item, ClaimInput) else item[1]
            for ref in (c.subject, c.object):
                if isinstance(ref, OriginRef) and ref.origin:
                    origins.add(ref.origin)
        with self.db.view() as conn:  # which origin ids exist on the target, one query per batch
            self._origin_hits = _canonicalize(conn, origins)
        try:
            return super().resolve(items)
        finally:
            self._origin_hits = {}

    def _resolve_ref(self, batch, ref, record, source_ref, evidence=None):  # type: ignore[override]
        origin = ref.origin if isinstance(ref, OriginRef) else None
        n = len(batch.res.logs)
        self._origin = origin
        try:
            resolved = super()._resolve_ref(batch, ref, record, source_ref, evidence)
        finally:
            self._origin = None
        if origin and len(batch.res.logs) > n and self.on_decision is not None:
            self.on_decision(origin, ref, batch.res.logs[n])
        return resolved

    # Identity-less refs are cached by origin, not by (kind, name): two different
    # nameless-key entities with the same name stay two entities.
    def _cached(self, batch: _Batch, r: _Ref, context_ids: tuple[str, ...]) -> Resolved | None:
        if not r.pairs and self._origin:
            return batch.__dict__.get("by_origin", {}).get(self._origin)
        return super()._cached(batch, r, context_ids)

    def _remember(self, batch: _Batch, r: _Ref, context_ids: tuple[str, ...], resolved: Resolved) -> None:
        if not r.pairs and self._origin:
            batch.__dict__.setdefault("by_origin", {})[self._origin] = resolved
            return
        super()._remember(batch, r, context_ids, resolved)

    def _mint(self, batch: _Batch, r: _Ref, record: str) -> _Decision:
        origin = self._origin
        if origin and origin.split(":")[1:2] == [r.kind]:
            existing = self._origin_hits.get(origin)
            if existing is not None and existing.kind == r.kind:
                batch.known.setdefault(existing.id, existing)
                return _Decision(
                    Resolved(existing.id, minted=False),
                    "matched",
                    1.0,
                    [{"pass": "origin_id", "origin": origin, "entity": existing.id}],
                )
            identity: dict[str, list[str]] = {}
            for k, v in r.pairs:
                identity.setdefault(k, []).append(v)
            # No minting key (identity-less, or only keys like x_id): the resolver would key the
            # id by record, which differs per claim. Keep the source's id: stable and idempotent.
            if strongest_key(identity) is None:
                return _Decision(
                    Resolved(origin, minted=True), "minted", 1.0, [{"pass": "mint", "basis": "origin_id"}]
                )
        return super()._mint(batch, r, record)

    def _pick(self, batch, r, candidates, evidence, *, confidence, how):  # type: ignore[override]
        """Several target entities match: if one of them is the bundle's own id (both instances
        derived it from the same key), use it rather than the oldest; still open the review item,
        since the target itself holds entities that share an identity value."""
        hit = self._origin_hits.get(self._origin) if self._origin else None
        if hit is not None and len(candidates) > 1 and any(c.id == hit.id for c in candidates):
            ids = sorted(c.id for c in candidates)
            review = ReviewRow(
                entity_ids=ids,
                reason=f"{len(ids)} {r.kind} entities match by {how}; used the imported id {hit.id}",
                evidence=evidence,
            )
            return _Decision(Resolved(hit.id, minted=False), "review", AMBIGUOUS_CONFIDENCE, evidence, review)
        return super()._pick(batch, r, candidates, evidence, confidence=confidence, how=how)

    def _add_name_claims(self, batch: _Batch) -> None:
        for eid, (record, ref, _ev) in batch.minted.items():
            if isinstance(ref, OriginRef) and ref.origin in self.named_origins:
                batch.named.add((record, eid))
        super()._add_name_claims(batch)


# ══ import: reporting ══════════════════════════════════════════════════════


@dataclass
class Outcome:
    origin: str
    kind: str
    target: str
    decision: str  # minted | matched | review
    how: str  # "x_handle=foo", "origin id", "same id", "new (x_handle)" ...
    name: str | None
    target_name: str | None = None

    @property
    def category(self) -> str:
        if self.decision == "minted":
            return "minted"
        if self.decision == "review":
            return "review"
        return "matched_same_id" if self.target == self.origin else "matched_other_id"


@dataclass
class Conflict:
    entity: str
    predicate: str
    target_value: Any
    target_layer: str
    incoming_value: Any
    layer: str
    incoming_wins: bool


@dataclass
class LayerCounts:
    claims: int = 0  # in the bundle (selected)
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    rejected: int = 0
    generated_names: int = 0  # name claims the resolver adds for minted entities named only elsewhere
    written: int = 0


@dataclass
class RunStats:
    claims: int = 0
    unchanged: int = 0
    written: int = 0
    minted: int = 0
    matched: int = 0
    review: int = 0
    rejected: int = 0
    batches: int = 0


@dataclass
class WorkerPlan:
    key: str  # bundle worker id, or "~<layer>" for claims without a worker
    bundle_id: str | None
    name: str
    kind: str
    source: str
    owner: str
    trust: float
    layers: list[str]
    config: dict[str, Any]
    description: str | None
    action: str = ""  # register | exists | add layers | conflict
    problem: str | None = None
    target_id: str | None = None
    target_trust: float | None = None
    run_id: int | None = None
    stats: RunStats = field(default_factory=RunStats)

    @property
    def label(self) -> str:
        return self.bundle_id or f"(no worker, {self.key[1:]}) -> {self.name}-{self.kind}"

    @property
    def write_trust(self) -> float:
        return self.target_trust if self.target_trust is not None else DEFAULT_TRUST.get(self.kind, 0.5)


@dataclass
class ImportOptions:
    dry_run: bool = False
    layers: Sequence[str] | None = None
    match_website: bool = False
    batch_claims: int = BATCH_CLAIMS
    examples: int = 10


@dataclass
class ImportReport:
    path: str
    dry_run: bool
    header: dict[str, Any]
    layers: dict[str, LayerCounts] = field(default_factory=dict)
    workers: list[WorkerPlan] = field(default_factory=list)
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)  # (layer, source_ref, reason)
    hints_dropped: Counter = field(default_factory=Counter)
    examples: int = 10
    aborted: str | None = None

    # ── summaries ──

    def entity_counts(self) -> dict[str, Counter]:
        out: dict[str, Counter] = defaultdict(Counter)
        for o in self.outcomes.values():
            out[o.kind][o.category] += 1
            out[o.kind]["total"] += 1
        return dict(sorted(out.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "dry_run": self.dry_run,
            "aborted": self.aborted,
            "source_instance": self.header.get("source_instance"),
            "exported_at": self.header.get("exported_at"),
            "layers": {k: asdict(v) for k, v in sorted(self.layers.items())},
            "workers": [
                {
                    "worker": w.label,
                    "target_id": w.target_id,
                    "action": w.action,
                    "problem": w.problem,
                    "run_id": w.run_id,
                    "stats": asdict(w.stats),
                }
                for w in self.workers
            ],
            "entities": {k: dict(v) for k, v in self.entity_counts().items()},
            "matches": [asdict(o) for o in self.match_examples(len(self.outcomes))],
            "review": [asdict(o) for o in self.outcomes.values() if o.decision == "review"],
            "conflicts": [asdict(c) for c in self.conflicts],
            "rejected": [list(r) for r in self.rejected],
            "hints_dropped": dict(self.hints_dropped),
        }

    def match_examples(self, n: int) -> list[Outcome]:
        other = [o for o in self.outcomes.values() if o.category == "matched_other_id"]
        return sorted(other, key=lambda o: (o.kind, o.how, o.origin))[:n]

    def render(self) -> str:
        n = self.examples
        h = self.header
        lines = [
            f"rebox import {self.path}" + ("  (DRY RUN: nothing written)" if self.dry_run else ""),
            f"bundle: from {h.get('source_instance')}, exported {h.get('exported_at')}, "
            f"layers {', '.join(sorted(self.layers))}",
        ]
        if self.aborted:
            lines.append(f"ABORTED: {self.aborted}")
        lines.append("")
        lines.append("workers:")
        for w in self.workers:
            what = w.action + (f" ({w.problem})" if w.problem else "")
            tgt = f" as {w.target_id}" if w.target_id and w.target_id != w.bundle_id else ""
            run = f"  run {w.run_id}: wrote {w.stats.written}" if w.run_id else ""
            lines.append(f"  {w.label:<28} {w.kind:<9} {w.source:<10} {what}{tgt}{run}")

        lines.append("")
        cols = ("claims", "new", "changed", "unchanged", "rejected", "generated_names")
        if not self.dry_run:
            cols = (*cols, "written")
        lines.append(f"  {'claims by layer':<22}" + "".join(f"{c:>17}" for c in cols))
        for layer, lc in sorted(self.layers.items()):
            lines.append(f"  {layer:<22}" + "".join(f"{getattr(lc, c):>17}" for c in cols))

        lines.append("")
        ecols = ("total", "minted", "matched_same_id", "matched_other_id", "review")
        lines.append(f"  {'entities':<22}" + "".join(f"{c:>17}" for c in ecols))
        for kind, cnt in self.entity_counts().items():
            lines.append(f"  {kind:<22}" + "".join(f"{cnt.get(c, 0):>17}" for c in ecols))
        lines.append(
            "  (minted = new on the target; matched_same_id = the target already has the entity under the "
            "same id; matched_other_id = merged onto an existing target entity with a different id)"
        )

        other = self.match_examples(len(self.outcomes))
        lines.append("")
        lines.append(
            f"matched onto existing entities under a different id: {len(other)}"
            + (f" (first {n})" if len(other) > n else "")
        )
        for o in other[:n]:
            tn = f" ({o.target_name})" if o.target_name and o.target_name != o.name else ""
            lines.append(f"  {o.origin} {o.name!r} -> {o.target}{tn}  by {o.how}")

        reviews = [o for o in self.outcomes.values() if o.decision == "review"]
        if reviews:
            lines.append("")
            lines.append(
                f"ambiguous (several target entities match; used the bundle's own id if among them, "
                f"else the oldest; opened a review item): "
                f"{len(reviews)}"
            )
            for o in reviews[:n]:
                lines.append(f"  {o.origin} {o.name!r} -> {o.target}  by {o.how}")

        wins = sum(c.incoming_wins for c in self.conflicts)
        by_pred = Counter(c.predicate for c in self.conflicts)
        lines.append("")
        lines.append(
            f"conflicts (one-valued attribute, different winning value on the target): {len(self.conflicts)}"
            + (f"; incoming would win {wins}" if self.conflicts else "")
            + (f"; by predicate {dict(by_pred.most_common())}" if self.conflicts else "")
        )
        for c in self.conflicts[:n]:
            verdict = "incoming wins" if c.incoming_wins else "target keeps"
            lines.append(
                f"  {c.entity} {c.predicate}: target {_short(c.target_value)} ({c.target_layer}) vs "
                f"incoming {_short(c.incoming_value)} ({c.layer})  [{verdict}]"
            )

        if self.hints_dropped:
            lines.append("")
            lines.append(
                "identity hints not used: "
                + ", ".join(f"{k} {v}" for k, v in sorted(self.hints_dropped.items()))
            )
        if self.rejected:
            lines.append("")
            lines.append(f"rejected claims: {len(self.rejected)}")
            for layer, ref, why in self.rejected[:n]:
                lines.append(f"  {layer} {ref}: {why}")
        return "\n".join(lines)


def _short(v: Any, limit: int = 60) -> str:
    s = json.dumps(v, ensure_ascii=False)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _how(row: LogRow, origin: str) -> str:
    keys = [f"{e['predicate']}={e['value']}" for e in row.evidence if e.get("pass") == "hard_key"]
    if keys:
        return ", ".join(sorted(set(keys)))
    passes = {e.get("pass") for e in row.evidence}
    if "origin_id" in passes:
        return "origin id"
    if "scoped_fuzzy" in passes:
        return "name within context"
    if any(e.get("existing") for e in row.evidence):
        return "same id" if row.entity_id == origin else "deterministic id"
    ident = row.identity.get("identity") or {}
    return "new" + (f" ({', '.join(sorted(ident))})" if ident else " (no identity key)")


# ══ import ═════════════════════════════════════════════════════════════════


def import_bundle(
    path: str | os.PathLike[str], db: Db, client: ClaimsClient | None, opts: ImportOptions | None = None
) -> ImportReport:
    """Import a bundle into the instance behind ``db`` (reads) and ``client`` (writes).

    With ``opts.dry_run`` only ``db`` is read and ``client`` may be None.
    Raises ``ImportAborted`` (after filling ``report.aborted``) before writing anything if a
    worker cannot be imported safely.
    """
    opts = opts or ImportOptions()
    if not opts.dry_run and client is None:
        raise ExchangeError("a writer client is required unless dry_run")
    return _Importer(Path(path), db, client, opts).run()


class _Importer:
    def __init__(self, path: Path, db: Db, client: ClaimsClient | None, opts: ImportOptions):
        self.path = path
        self.db = db
        self.client = client
        self.opts = opts
        header = read_header(path)
        self.header = header
        selected = list(opts.layers) if opts.layers else list(header.get("layers") or [])
        unknown = sorted(set(selected) - set(header.get("layers") or []))
        if unknown:
            raise ExchangeError(f"layers not in the bundle: {', '.join(unknown)}")
        self.selected = set(selected)
        self.report = ImportReport(
            path=str(path), dry_run=opts.dry_run, header=header, examples=opts.examples
        )
        with db.view() as conn:
            self.predicates: dict[str, Predicate] = load_predicates(conn)
        self.resolvers: dict[tuple[str, str], ImportResolver] = {}
        self.plans: dict[str, WorkerPlan] = {}
        self.refs: dict[str, OriginRef] = {}
        self.shared: set[tuple[str, str, str]] = set()
        self.named: set[str] = set()  # origins with a name claim among the imported claims
        self.preexisting: set[str] = set()  # target ids matched (not minted) by this import

    # ── passes ──

    def run(self) -> ImportReport:
        self._scan()
        self._plan_workers()
        problems = [w for w in self.report.workers if w.problem]
        if problems:
            self.report.aborted = "; ".join(f"{w.label}: {w.problem}" for w in problems)
            if self.opts.dry_run:
                return self.report
            raise ImportAborted(self.report.aborted)
        if not self.opts.dry_run:
            self._register_workers()
        error: BaseException | None = None
        try:
            self._stream()
        except BaseException as e:
            error = e
            raise
        finally:
            self._close_runs(error)
        self._target_names()
        return self.report

    def _scan(self) -> None:
        """First pass: layer counts, workers used, names, and identity values shared by origins."""
        claims = iter_claims(self.path)
        first: dict[tuple[str, str, str], str] = {}
        self.used_workers: dict[str, str] = {}  # plan key -> a layer it writes
        for c in claims:
            layer = c["layer"]
            if layer not in self.selected:
                continue
            self.report.layers.setdefault(layer, LayerCounts()).claims += 1
            key = c["worker"] or f"~{layer}"
            self.used_workers.setdefault(key, layer)
            if c["predicate"] == "name" and not c.get("retract") and c["object"] is None:
                self.named.add(c["subject"]["origin_id"])
            for ref in (c["subject"], c["object"]):
                if not ref:
                    continue
                for p, vals in (ref.get("identity") or {}).items():
                    for v in vals if isinstance(vals, list) else [vals]:
                        k = (ref["kind"], p, str(v))
                        o = first.setdefault(k, ref["origin_id"])
                        if o != ref["origin_id"]:
                            self.shared.add(k)
        for layer in self.selected:
            self.report.layers.setdefault(layer, LayerCounts())

    def _plan_workers(self) -> None:
        by_id = {w["id"]: w for w in self.header.get("workers") or []}
        with self.db.view() as conn:
            for key, layer in sorted(self.used_workers.items()):
                if key.startswith("~"):
                    source = layer.split("/", 1)[1] if layer.startswith("public/") else layer
                    plan = WorkerPlan(
                        key=key,
                        bundle_id=None,
                        name=worker_name(source),
                        kind="connector",
                        source=source,
                        owner="rebox",
                        trust=DEFAULT_TRUST["connector"],
                        layers=[layer],
                        config={},
                        description=None,
                    )
                    row = conn.execute(
                        """select id, kind, source, owner, trust, layers, status from worker
                            where kind = 'connector' and source = %s and owner = 'rebox' and id ~ %s
                            order by created_at, id limit 1""",
                        (source, f"^{plan.name}-connector-[0-9]{{6}}$"),
                    ).fetchone()
                else:
                    w = by_id.get(key)
                    if w is None:
                        raise ExchangeError(f"bundle claims name worker {key!r}, which is not in the header")
                    m = _WORKER_ID_RE.match(key)
                    plan = WorkerPlan(
                        key=key,
                        bundle_id=key,
                        name=m.group("name") if m else "",
                        kind=w["kind"],
                        source=w["source"],
                        owner=w.get("owner") or "rebox",
                        trust=float(w.get("trust") or DEFAULT_TRUST.get(w["kind"], 0.5)),
                        layers=list(w.get("layers") or [layer]),
                        config=dict(w.get("config") or {}),
                        description=w.get("description"),
                    )
                    if not m or m.group("kind") != plan.kind:
                        plan.problem = f"id does not have the form <name>-{plan.kind}-<6 digits>"
                    elif plan.kind == "external" or plan.owner != "rebox":
                        plan.problem = (
                            "external workers cannot be registered with the ingest token; "
                            "create it on the target first (admin) or leave its layer out (--layers)"
                        )
                    row = conn.execute(
                        "select id, kind, source, owner, trust, layers, status from worker where id = %s",
                        (key,),
                    ).fetchone()
                self.plans[key] = plan
                self.report.workers.append(plan)
                if row is None:
                    plan.action = "register (new)"
                    continue
                plan.target_id = row["id"]
                plan.target_trust = float(row["trust"])
                if (row["kind"], row["source"], row["owner"]) != (plan.kind, plan.source, "rebox"):
                    plan.problem = plan.problem or (
                        f"target already has {row['id']} as {row['kind']}/{row['source']}/{row['owner']}, "
                        f"bundle has {plan.kind}/{plan.source}/{plan.owner}"
                    )
                    plan.action = "conflict"
                    continue
                if row["status"] != "active":
                    plan.problem = plan.problem or f"worker {row['id']} is {row['status']} on the target"
                missing = [x for x in plan.layers if x not in row["layers"]]
                plan.action = "exists" + (f", add layers {missing}" if missing else "")
                if plan.bundle_id and abs(plan.target_trust - plan.trust) > 1e-6:
                    plan.action += f", trust {plan.target_trust} on target vs {plan.trust} in bundle"

    def _register_workers(self) -> None:
        assert self.client is not None
        for plan in self.report.workers:
            body: dict[str, Any] = {
                "name": plan.name,
                "kind": plan.kind,
                "source": plan.source,
                "layers": plan.layers,
            }
            if plan.config:
                body["config"] = plan.config
            if plan.description:
                body["description"] = plan.description
            if plan.bundle_id:
                body["id"] = plan.bundle_id
            # ClaimsClient.register_worker has no `id` parameter; same endpoint, same retries.
            got = str(self.client._request("POST", "/internal/workers", body)["id"])
            if plan.bundle_id and got != plan.bundle_id:
                raise ImportAborted(f"registered {plan.bundle_id} but the target returned {got}")
            plan.target_id = got

    # ── streaming ──

    def _stream(self) -> None:
        claims = iter_claims(self.path)
        batch: list[dict[str, Any]] = []
        key: tuple[str, str] | None = None
        for c in claims:
            if c["layer"] not in self.selected:
                continue
            k = (c["layer"], c["worker"] or f"~{c['layer']}")
            if batch and (k != key or len(batch) >= self.opts.batch_claims):
                self._batch(key, batch)  # type: ignore[arg-type]
                batch = []
            key = k
            batch.append(c)
        if batch:
            self._batch(key, batch)  # type: ignore[arg-type]

    def _ref(self, d: dict[str, Any]) -> OriginRef:
        origin = d["origin_id"]
        ref = self.refs.get(origin)
        if ref is not None:
            return ref
        kind = d["kind"]
        ident: dict[str, Any] = {}
        for p, vals in sorted((d.get("identity") or {}).items()):
            if p in WEAK_HINT_KEYS and not self.opts.match_website:
                self.report.hints_dropped[f"{p} (weak key)"] += 1
                continue
            keep = []
            for v in vals if isinstance(vals, list) else [vals]:
                if (kind, p, str(v)) in self.shared:
                    self.report.hints_dropped[f"{p} (shared by several origin entities)"] += 1
                else:
                    keep.append(v)
            if keep:
                ident[p] = keep[0] if len(keep) == 1 else keep
        ref = OriginRef.of(kind, ident, d.get("name"), origin)
        self.refs[origin] = ref
        return ref

    def _resolver(self, layer: str, source: str) -> ImportResolver:
        r = self.resolvers.get((layer, source))
        if r is None:
            r = ImportResolver(
                self.db,
                source=source,
                layer=layer,
                predicates=self.predicates,
                named_origins=self.named,
                on_decision=self._decided,
            )
            self.resolvers[(layer, source)] = r
        return r

    def _decided(self, origin: str, ref: OriginRef, row: LogRow) -> None:
        if origin in self.report.outcomes:
            return  # first decision wins (later batches re-find what an earlier batch minted)
        self.report.outcomes[origin] = Outcome(
            origin=origin,
            kind=row.kind,
            target=row.entity_id,
            decision=row.decision,
            how=_how(row, origin),
            name=ref.name,
        )
        if row.decision != "minted":
            self.preexisting.add(row.entity_id)

    def _batch(self, key: tuple[str, str], raw: list[dict[str, Any]]) -> None:
        layer, wkey = key
        plan = self.plans[wkey]
        lc = self.report.layers[layer]
        resolver = self._resolver(layer, plan.source)

        items: list[ClaimInput] = []
        for c in raw:
            try:
                items.append(
                    ClaimInput(
                        subject=self._ref(c["subject"]),
                        predicate=c["predicate"],
                        object=self._ref(c["object"]) if c.get("object") else None,
                        value=c.get("value"),
                        valid_from=c.get("valid_from"),
                        valid_to=c.get("valid_to"),
                        retract=bool(c.get("retract")),
                        source_ref=c["source_ref"],
                        evidence=[Evidence(**e) for e in c["evidence"]] if c.get("evidence") else None,
                    )
                )
            except (ValidationError, KeyError, TypeError) as e:
                self._reject(layer, plan, str(c.get("source_ref")), f"invalid bundle claim: {e}")

        res = resolver.resolve(items)
        for ref, why in res.rejected:
            self._reject(layer, plan, ref, why)
        lc.generated_names += len(res.claims) - (len(items) - len(res.rejected))

        send, unchanged = self._diff(layer, res.claims)
        lc.unchanged += unchanged
        self._find_conflicts(layer, plan, res.claims)

        st = plan.stats
        st.claims += len(res.claims)
        st.unchanged += unchanged
        st.minted += res.count("minted")
        st.matched += res.count("matched")
        st.review += res.count("review")
        if self.opts.dry_run or not send:
            return
        assert self.client is not None and plan.target_id is not None
        if plan.run_id is None:
            plan.run_id = self.client.open_run(plan.target_id, "replay")
            log.info("import: writing as %s (run %d)", plan.target_id, plan.run_id)
        written = self._write(layer, plan, send)
        resolver.commit(res)  # resolution_log + review items, after the writer accepted the claims
        lc.written += written
        st.written += written
        st.batches += 1
        try:
            self.client.update_run(plan.run_id, stats=self._run_stats(plan))
        except Exception as e:  # noqa: BLE001 - progress is best effort
            log.warning("run %d: progress update failed: %s", plan.run_id, e)
        log.info(
            "import: %s %s: %d claims, %d sent, %d written",
            layer,
            plan.target_id,
            len(res.claims),
            len(send),
            written,
        )

    def _reject(self, layer: str, plan: WorkerPlan, source_ref: str, why: str) -> None:
        self.report.layers[layer].rejected += 1
        plan.stats.rejected += 1
        self.report.rejected.append((layer, source_ref, why))
        log.warning("import: rejected %s %s: %s", layer, source_ref, why)

    def _diff(self, layer: str, claims: list[ClaimWrite]) -> tuple[list[ClaimWrite], int]:
        """Claims that would change the target (new or changed), and how many are unchanged.

        Mirrors the writer's upsert: same (layer, source_ref, subject, predicate, object) with the
        same value, validity, retract flag and evidence, already attributed to a worker = no-op.
        """
        lc = self.report.layers[layer]
        refs = sorted({c.source_ref for c in claims})
        existing: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        if refs:
            with self.db.view() as conn:
                for r in conn.execute(
                    """select source_ref, subject, predicate, coalesce(object_id, '') as obj, value,
                              valid_from, valid_to, retract, evidence, worker_id
                         from claim where layer_id = %s and source_ref = any(%s)""",
                    (layer, refs),
                ):
                    existing[(r["source_ref"], r["subject"], r["predicate"], r["obj"])] = r
        send: list[ClaimWrite] = []
        unchanged = 0
        for c in claims:
            r = existing.get((c.source_ref, c.subject_id, c.predicate, c.object_id or ""))
            if r is None:
                lc.new += 1
                send.append(c)
            elif _same(r, c):
                unchanged += 1
            else:
                lc.changed += 1
                send.append(c)
        return send, unchanged

    def _find_conflicts(self, layer: str, plan: WorkerPlan, claims: list[ClaimWrite]) -> None:
        cands = [
            c
            for c in claims
            if not c.retract
            and c.object is None
            and isinstance(c.subject, IdRef)
            and c.subject.id in self.preexisting
            and (p := self.predicates.get(c.predicate)) is not None
            and p.cardinality == "one"
        ]
        if not cands:
            return
        runs = [w.run_id for w in self.report.workers if w.run_id is not None]
        with self.db.view() as conn:
            rows = conn.execute(
                """select distinct on (c.subject, c.predicate)
                          c.subject, c.predicate, c.value, c.layer_id, c.source_ref,
                          coalesce(w.trust, %s) as trust
                     from claim c left join worker w on w.id = c.worker_id
                    where c.subject = any(%s) and c.predicate = any(%s) and c.object_id is null
                      and not c.retract and (c.run_id is null or c.run_id <> all(%s::bigint[]))
                    order by c.subject, c.predicate, coalesce(w.trust, %s) desc,
                             c.updated_at desc, c.id desc""",
                (
                    UNATTRIBUTED_TRUST,
                    sorted({c.subject_id for c in cands}),
                    sorted({c.predicate for c in cands}),
                    runs,
                    UNATTRIBUTED_TRUST,
                ),
            ).fetchall()
        winners = {(r["subject"], r["predicate"]): r for r in rows}
        seen = {(x.entity, x.predicate) for x in self.report.conflicts}
        for c in cands:
            w = winners.get((c.subject_id, c.predicate))
            if (
                w is None
                or w["value"] == c.value
                or (w["layer_id"] == layer and w["source_ref"] == c.source_ref)
            ):
                continue
            if (c.subject_id, c.predicate) in seen:
                continue
            seen.add((c.subject_id, c.predicate))
            self.report.conflicts.append(
                Conflict(
                    entity=c.subject_id,
                    predicate=c.predicate,
                    target_value=w["value"],
                    target_layer=w["layer_id"],
                    incoming_value=c.value,
                    layer=layer,
                    # Same scope (public); higher trust wins, a tie goes to the newer claim (ours).
                    incoming_wins=plan.write_trust >= float(w["trust"]) - 1e-6,
                )
            )

    def _write(self, layer: str, plan: WorkerPlan, claims: list[ClaimWrite]) -> int:
        """Write in writer-sized batches. A 400 that names claims drops those claims (reported as
        rejected) and retries the rest once; anything else propagates."""
        assert self.client is not None and plan.target_id is not None
        written = 0
        for chunk in chunks(claims, BATCH_SIZE):
            chunk = list(chunk)
            try:
                written += self.client.write_batch(
                    layer, chunk, worker=plan.target_id, run_id=plan.run_id
                ).written
                continue
            except ValidationRejected as e:
                bad = {d.get("index"): d.get("message") for d in e.details}
                if not bad or any(not isinstance(i, int) or not 0 <= i < len(chunk) for i in bad):
                    raise
                for i, msg in sorted(bad.items()):
                    self._reject(layer, plan, chunk[i].source_ref, f"writer: {msg}")
                keep = [c for i, c in enumerate(chunk) if i not in bad]
            if keep:
                written += self.client.write_batch(
                    layer, keep, worker=plan.target_id, run_id=plan.run_id
                ).written
        return written

    def _run_stats(self, plan: WorkerPlan) -> dict[str, Any]:
        return {
            **asdict(plan.stats),
            "import": {
                "bundle": self.path.name,
                "source_instance": self.header.get("source_instance"),
                "exported_at": self.header.get("exported_at"),
                "origin_worker": plan.bundle_id,
            },
        }

    def _close_runs(self, error: BaseException | None) -> None:
        if self.client is None:
            return
        for plan in self.report.workers:
            if plan.run_id is None:
                continue
            try:
                self.client.update_run(
                    plan.run_id,
                    status="ok" if error is None else "failed",
                    stats=self._run_stats(plan),
                    error=None if error is None else f"{type(error).__name__}: {error}",
                )
            except Exception as e:  # noqa: BLE001 - never mask the import's own error
                log.error("run %d: could not close: %s", plan.run_id, e)

    def _target_names(self) -> None:
        shown = (
            self.report.match_examples(self.opts.examples)
            + [o for o in self.report.outcomes.values() if o.decision == "review"][: self.opts.examples]
        )
        ids = sorted({o.target for o in shown})
        if not ids:
            return
        with self.db.view() as conn:
            names: dict[str, str] = {}
            for r in conn.execute(
                """select c.subject, c.value_text from claim c left join worker w on w.id = c.worker_id
                    where c.subject = any(%s) and c.predicate = 'name' and c.object_id is null
                      and not c.retract
                    order by coalesce(w.trust, %s) desc, c.updated_at desc""",
                (ids, UNATTRIBUTED_TRUST),
            ):
                names.setdefault(r["subject"], r["value_text"])
        for o in shown:
            o.target_name = names.get(o.target)


def _ts(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else None  # a naive value depends on the writer's session time zone


def _same(row: dict[str, Any], c: ClaimWrite) -> bool:
    if row["worker_id"] is None:  # the writer re-attributes unattributed claims
        return False
    if row["value"] != c.value or bool(row["retract"]) != c.retract:
        return False
    for col, v in (("valid_from", c.valid_from), ("valid_to", c.valid_to)):
        if (row[col] is None) != (v is None):
            return False
        if v is not None and _ts(v) != row[col]:
            return False
    evidence = [e.to_request() for e in c.evidence] if c.evidence else None
    return (row["evidence"] or None) == evidence
