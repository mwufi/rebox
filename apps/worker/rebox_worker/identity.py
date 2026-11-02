"""Worker identity and run tracking (CONTRACTS §12).

Every write names a **worker** (``<name>-<kind>-<6 digits>``) and the **run** it belongs to.

Identity
    A connector writes as ``<connector>-connector-<6 digits>`` by default (``x_spider`` becomes
    ``x-spider``). A connector picks another identity with optional attributes::

        worker_name = "dbooth"        # default: the connector name with '_' -> '-'
        worker_kind = "spider"        # connector | spider | parser | enricher (default connector)

    or a method ``worker_spec(ctx) -> WorkerSpec`` (e.g. to put its seed list in ``config``).

    A connector that fetches on behalf of several identities (e.g. ``x_spider``: one per seed set)
    defines ``workers(ctx) -> [ {name, kind?, source?, layers?, config?, description?} ]`` (or
    ``WorkerSpec`` objects) and ``worker_for(rec) -> str | dict | WorkerSpec | None``. A ``str`` is
    the name of one of ``workers()``. Every listed worker is registered when the run starts. Each
    record is written as the worker ``worker_for`` names, in a run of that worker's own (opened on
    first use); ``None`` (or no hook) means the connector-level identity. A write batch never
    mixes workers. If ``workers()`` lists the connector-level name itself, that entry (with its
    config/description) is the connector-level identity.

Run stats
    Besides the pipeline's counters and ``ctx.add_usage``, a connector may expose ``run_stats``
    (a dict, or a method returning one: ``api_calls``, ``cost_usd``, ``user_reads``, ``x_stop``,
    ``budget_stop``, ...). It is merged into the connector-level run's stats at every progress
    update and when the run closes (ok or failed).

Trust (``config/trust.yaml``, override the path with ``$TRUST_FILE``)
    Which value the fold shows when sources disagree depends on worker trust (CONTRACTS §12). A
    worker's table trust is ``sources[<its source>]`` if listed, else ``defaults[<its kind>]``
    (``trust_for``). ``register`` passes it to ``ClaimsClient.register_worker(trust=...)``, which
    applies it to a new worker, and to an existing one whose trust is still its kind default (or
    the value the table set before); a trust an admin changed is never touched. Applying it needs
    ``ADMIN_TOKEN``; without one the client logs a one-line hint. If the file is missing (e.g. an
    image without ``config/``) the built-in copy of the table is used.

Persistence
    The id lives in the ``worker`` table, not in ``connector_state``: ``POST /internal/workers``
    is idempotent on ``(name, kind, source)``, so every run resolves the same id with one call
    (which also adds a new layer to the worker if the connector started writing one). Wiping
    ``connector_state`` therefore never changes a worker's identity; the id only changes if the
    worker row is deleted.

Runs
    ``RunTracker`` opens a run per identity on first use (kind ``sync``/``replay``, or ``parse``
    / ``enrich`` for parser and enricher workers), PATCHes progress after each flush and closes
    every run with its final stats, as ``failed`` with the error if the sync raised.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .client import DEFAULT_TRUST

if TYPE_CHECKING:
    from .client import ClaimsClient
    from .connectors.base import Connector, Ctx
    from .models import RawRecord

log = logging.getLogger(__name__)

INTERNAL_KINDS = ("connector", "spider", "parser", "enricher")
_NAME_BAD = re.compile(r"[^a-z0-9-]+")


def worker_name(raw: str) -> str:
    """A valid worker name: lowercase, [a-z0-9-], at most 40 characters."""
    name = _NAME_BAD.sub("-", raw.lower()).strip("-")[:40].strip("-")
    if not name:
        raise ValueError(f"cannot make a worker name from {raw!r}")
    return name


@dataclass(frozen=True)
class WorkerSpec:
    """What makes a worker distinct. ``source`` and ``layers`` default to the connector's."""

    name: str
    kind: str = "connector"
    source: str = ""
    layers: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict, compare=False, hash=False)
    description: str | None = field(default=None, compare=False, hash=False)

    def __post_init__(self) -> None:
        if self.kind not in INTERNAL_KINDS:
            raise ValueError(f"worker kind must be one of {INTERNAL_KINDS}, got {self.kind!r}")
        object.__setattr__(self, "name", worker_name(self.name))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.name, self.kind, self.source)


def as_spec(value: Any) -> WorkerSpec:
    """A WorkerSpec from a ``workers()`` entry: a WorkerSpec, or a dict with at least ``name``."""
    if isinstance(value, WorkerSpec):
        return value
    if isinstance(value, Mapping) and value.get("name"):
        return WorkerSpec(
            name=str(value["name"]),
            kind=str(value.get("kind") or "connector"),
            source=str(value.get("source") or ""),
            layers=tuple(value.get("layers") or ()),
            config=dict(value.get("config") or {}),
            description=value.get("description"),
        )
    raise TypeError(f"not a worker identity: {value!r}")


def listed_workers(connector: Connector, ctx: Ctx | None) -> list[WorkerSpec]:
    """The identities a connector declares with ``workers(ctx)`` (defaults filled in)."""
    hook = getattr(connector, "workers", None)
    if not callable(hook):
        return []
    out = []
    for w in hook(ctx) or []:
        spec = as_spec(w)
        if not spec.kind or (isinstance(w, Mapping) and not w.get("kind")):
            spec = WorkerSpec(
                name=spec.name,
                kind=getattr(connector, "worker_kind", None) or "connector",
                source=spec.source,
                layers=spec.layers,
                config=spec.config,
                description=spec.description,
            )
        out.append(spec_for(connector, ctx, spec))
    return out


def connector_run_stats(connector: Connector) -> dict[str, Any]:
    """The connector's own ``run_stats`` (attribute dict or method), or {}."""
    rs = getattr(connector, "run_stats", None)
    if callable(rs):
        rs = rs()
    return dict(rs) if isinstance(rs, Mapping) else {}


def spec_for(connector: Connector, ctx: Ctx | None = None, spec: WorkerSpec | None = None) -> WorkerSpec:
    """The connector's main identity (or ``spec`` from ``worker_for``) with defaults filled in."""
    if spec is None:
        custom = getattr(connector, "worker_spec", None)
        if callable(custom):
            spec = custom(ctx)
        else:
            spec = WorkerSpec(
                name=getattr(connector, "worker_name", None) or connector.name,
                kind=getattr(connector, "worker_kind", None) or "connector",
            )
    assert spec is not None
    return WorkerSpec(
        name=spec.name,
        kind=spec.kind,
        source=spec.source or connector.name,
        layers=spec.layers or (connector.layer,),
        config=spec.config,
        description=spec.description,
    )


# ── source trust table ──

# Built-in copy of config/trust.yaml, used when the file is not there. Keep the three in sync
# (this, the file, db/migrations/0008_legacy_workers.sql); tests check that they agree.
BUILTIN_SOURCE_TRUST: dict[str, float] = {
    "wikidata": 0.9,
    "github": 0.8,
    "hn": 0.7,
    "x_spider": 0.75,
    "exa": 0.7,
}


@dataclass(frozen=True)
class TrustTable:
    """Default trust by worker kind, with overrides by worker source."""

    defaults: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_TRUST))
    sources: Mapping[str, float] = field(default_factory=lambda: dict(BUILTIN_SOURCE_TRUST))
    path: Path | None = None

    def trust_for(self, kind: str, source: str) -> float:
        if source in self.sources:
            return self.sources[source]
        if kind in self.defaults:
            return self.defaults[kind]
        return DEFAULT_TRUST.get(kind, 0.5)


def trust_path(env: Mapping[str, str] | None = None) -> Path:
    """``$TRUST_FILE`` if set, else ``config/trust.yaml`` in the nearest ancestor that has it."""
    env = os.environ if env is None else env
    if env.get("TRUST_FILE"):
        return Path(env["TRUST_FILE"]).expanduser()
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "trust.yaml").is_file():
            return parent / "config" / "trust.yaml"
    return Path.cwd() / "config" / "trust.yaml"


def _trust_map(raw: Any, where: str) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: expected a mapping of name -> trust")
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            t = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{where}.{k}: trust must be a number, got {v!r}") from None
        if not 0 <= t <= 1:
            raise ValueError(f"{where}.{k}: trust must be between 0 and 1, got {t}")
        out[str(k)] = t
    return out


def load_trust(env: Mapping[str, str] | None = None, path: Path | None = None) -> TrustTable:
    """The trust table from ``path`` / ``trust_path(env)``; the built-in table if there is no file."""
    from .frontier import parse_yaml  # the worker's small YAML subset (no YAML dependency)

    p = path or trust_path(env)
    if not p.is_file():
        if path is not None or (env if env is not None else os.environ).get("TRUST_FILE"):
            raise FileNotFoundError(f"trust table {p} not found")
        return TrustTable()
    data = parse_yaml(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ValueError(f"{p}: expected a mapping at the top level")
    defaults = {**DEFAULT_TRUST, **_trust_map(data.get("defaults"), f"{p}: defaults")}
    return TrustTable(defaults=defaults, sources=_trust_map(data.get("sources"), f"{p}: sources"), path=p)


_TRUST_CACHE: dict[Path, tuple[float, TrustTable]] = {}


def trust_table() -> TrustTable:
    """The current table (re-read when the file changes)."""
    p = trust_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return load_trust(path=None)
    hit = _TRUST_CACHE.get(p)
    if hit is None or hit[0] != mtime:
        hit = (mtime, load_trust(path=p))
        _TRUST_CACHE[p] = hit
    return hit[1]


def trust_for(kind: str, source: str) -> float:
    """The table trust for a worker of this kind and source."""
    return trust_table().trust_for(kind, source)


def register(client: ClaimsClient, spec: WorkerSpec, trust: float | None = None) -> str:
    """Register ``spec`` with its table trust (``trust_for``) unless ``trust`` is given. An
    unreadable trust table never stops a sync: the worker registers without a trust."""
    if trust is None:
        try:
            trust = trust_for(spec.kind, spec.source)
        except (OSError, ValueError) as e:
            log.warning("trust table: %s; registering %s without a table trust", e, spec.name)
    return client.register_worker(
        name=spec.name,
        kind=spec.kind,
        source=spec.source,
        layers=spec.layers,
        config=dict(spec.config) or None,
        description=spec.description,
        trust=trust,
    )


def run_kind(spec: WorkerSpec, pipeline_kind: str) -> str:
    if pipeline_kind == "replay":
        return "replay"
    return {"parser": "parse", "enricher": "enrich"}.get(spec.kind, "sync")


@dataclass
class Active:
    """One worker identity in use during a sync/replay, with its open run."""

    spec: WorkerSpec
    worker_id: str
    run_id: int
    stats: Any  # pipeline.Stats of the batches written under this run


class RunTracker:
    """Opens, updates and closes the runs of one sync/replay (one per worker identity used)."""

    def __init__(self, client: ClaimsClient, connector: Connector, ctx: Ctx | None, kind: str, new_stats):
        self.client = client
        self.connector = connector
        self.kind = kind
        self.new_stats = new_stats
        self.active: dict[tuple[str, str, str], Active] = {}
        self.ids: dict[tuple[str, str, str], str] = {}  # registered worker ids
        main = spec_for(connector, ctx)
        # Register every declared identity up front (so seed workers exist even before they
        # write); runs are opened lazily, on each identity's first batch.
        self.listed: dict[str, WorkerSpec] = {}
        try:
            listed = listed_workers(connector, ctx)
        except Exception as e:  # noqa: BLE001 - e.g. an unreadable seeds file; resolved lazily
            log.warning("%s: could not list workers: %s", connector.name, e)
            listed = []
        for spec in listed:
            self.listed[spec.name] = spec
            if spec.name == main.name and spec.kind == main.kind:
                main = spec
            self._register(spec)
        self.main = self._open(main)

    def _register(self, spec: WorkerSpec) -> str:
        if spec.key not in self.ids:
            self.ids[spec.key] = register(self.client, spec)
        return self.ids[spec.key]

    def _open(self, spec: WorkerSpec) -> Active:
        worker_id = self._register(spec)
        run_id = self.client.open_run(worker_id, run_kind(spec, self.kind))
        a = Active(spec, worker_id, run_id, self.new_stats())
        self.active[spec.key] = a
        log.info("%s: writing as %s (run %d)", self.connector.name, worker_id, run_id)
        return a

    def spec_of(self, value: Any) -> WorkerSpec | None:
        """What ``worker_for`` returned, as a full spec (None = the connector-level identity)."""
        if value is None or value == "":
            return None
        if isinstance(value, str):
            spec = self.listed.get(worker_name(value)) or WorkerSpec(
                name=value, kind=getattr(self.connector, "worker_kind", None) or "connector"
            )
        else:
            spec = as_spec(value)
        return spec_for(self.connector, None, spec)

    def for_record(self, rec: RawRecord) -> Active:
        hook = getattr(self.connector, "worker_for", None)
        spec = self.spec_of(hook(rec)) if callable(hook) else None
        if spec is None or spec.key == self.main.spec.key:
            return self.main
        return self.active.get(spec.key) or self._open(spec)

    def run_stats(self, a: Active, totals: Any) -> dict[str, Any]:
        """Stats for one run. Fetch-level counters (fetched, skipped, errors, usage and the
        connector's own ``run_stats``) belong to the connector-level run; every run gets the
        write counters of its own batches."""
        out = a.stats.as_dict()
        if a is self.main:
            for k in ("fetched", "skipped_unchanged", "extract_errors"):
                out[k] = getattr(totals, k)
            out.update(totals.usage)
            out.update(connector_run_stats(self.connector))
        return out

    def progress(self, totals: Any) -> None:
        """Best-effort live stats for the explorer; never fails the sync."""
        for a in self.active.values():
            try:
                self.client.update_run(a.run_id, stats=self.run_stats(a, totals))
            except Exception as e:  # noqa: BLE001
                log.warning("run %d: progress update failed: %s", a.run_id, e)

    def close(self, totals: Any, error: BaseException | None) -> None:
        """Close every run: ok, or failed with the error. Never masks the sync's own exception."""
        for a in self.active.values():
            status = "ok" if error is None else "failed"
            err = None if error is None else f"{type(error).__name__}: {error}"
            try:
                self.client.update_run(a.run_id, status=status, stats=self.run_stats(a, totals), error=err)
            except Exception as e:  # noqa: BLE001
                log.error("run %d: could not close as %s: %s", a.run_id, status, e)
