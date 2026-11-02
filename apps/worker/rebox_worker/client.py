"""Writer API client (CONTRACTS §5, §12).

``POST /internal/claims`` (every batch names its ``worker`` and ``run_id``), ``POST /internal/workers``
(idempotent registration), ``POST /internal/runs`` and ``PATCH /internal/runs/:id``. With an admin
token (``admin_token=``, else ``$ADMIN_TOKEN``) also ``PATCH /v0/admin/workers/:id`` (trust plus
the ``trust_source``/``trust_table`` config marker), used only to apply a worker's table trust
(see ``register_worker``).

Rejections
    The writer is all-or-nothing per batch: a ``400`` lists the bad claims by index and writes
    nothing. With ``write(..., drop_rejected=True)`` (what the pipeline uses) the client drops
    exactly those claims, plus any claim that refers by ``{"id"}`` to an entity only a dropped
    claim would have minted, logs each with its reason, and re-sends the rest, so one bad record
    never stops an indexer. They come back in ``WriteResult.rejected``. A ``400`` that is not
    about specific claims (index ``-1``: worker not active, layer not allowed, ...) still raises
    ``ValidationRejected``, as do auth errors (``WriterError``) and transport/5xx/429 errors once
    the retries are used up.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .models import ClaimWrite, IdRef, MintRef

log = logging.getLogger(__name__)

BATCH_SIZE = 500  # API max is 1000 per request

# CONTRACTS §12 default trust by worker kind: what POST /internal/workers assigns a new worker.
DEFAULT_TRUST: dict[str, float] = {
    "connector": 0.8,
    "spider": 0.8,
    "parser": 0.6,
    "enricher": 0.5,
    "external": 0.3,
}
_TRUST_EPS = 1e-4  # worker.trust is a float4


def _same_trust(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) < _TRUST_EPS
    except (TypeError, ValueError):
        return False


class WriterError(RuntimeError):
    pass


class ValidationRejected(WriterError):
    """400 from the writer: nothing in the batch was written."""

    def __init__(self, message: str, details: list[dict[str, Any]]):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class RejectedWrite:
    """A claim the writer refused (or that depended on one), with the writer's reason."""

    claim: ClaimWrite
    reason: str


@dataclass
class WriteResult:
    written: int = 0
    minted: int = 0
    rejected: list[RejectedWrite] = field(default_factory=list)

    def __iadd__(self, other: WriteResult) -> WriteResult:
        self.written += other.written
        self.minted += other.minted
        self.rejected.extend(other.rejected)
        return self


def minted_ids(c: ClaimWrite) -> set[str]:
    """Ids this claim mints (its ``{"mint"}`` refs)."""
    return {ref.mint.id for ref in (c.subject, c.object) if ref is not None and isinstance(ref, MintRef)}


def _id_refs(c: ClaimWrite) -> set[str]:
    """Ids this claim needs to exist already (its ``{"id"}`` refs)."""
    return {ref.id for ref in (c.subject, c.object) if ref is not None and isinstance(ref, IdRef)}


@dataclass
class _MintLedger:
    """Across one ``write`` call: ids minted by written claims, and ids in refused claims' mints."""

    written: set[str] = field(default_factory=set)
    refused: set[str] = field(default_factory=set)


def chunks(items: Sequence[ClaimWrite], size: int = BATCH_SIZE) -> Iterator[Sequence[ClaimWrite]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class ClaimsClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        user_agent: str = "rebox-bot/0.1",
        max_retries: int = 5,
        backoff_s: float = 0.5,
        max_backoff_s: float = 30.0,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        admin_token: str | None = None,
    ) -> None:
        # Only for PATCH /v0/admin/workers/:id (table trust); never logged.
        # admin_token="" means none, even if $ADMIN_TOKEN is set.
        token_ = admin_token if admin_token is not None else os.environ.get("ADMIN_TOKEN")
        self.admin_token = token_ or None
        # Worker id -> table trust already reconciled by this client: a long-running worker
        # registers its identities on every sync, but reads (GET /v0/workers/:id) and hints once.
        self._trust_done: dict[str, float] = {}
        self.http = httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
            timeout=timeout_s,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.sleep = sleep

    def write(
        self,
        layer: str,
        claims: Sequence[ClaimWrite],
        *,
        worker: str,
        run_id: int | None = None,
        batch_size: int = BATCH_SIZE,
        drop_rejected: bool = False,
    ) -> WriteResult:
        """Write ``claims`` as ``worker`` (in ``run_id``) in batches of at most ``batch_size``;
        each batch is one transaction. With ``drop_rejected``, claims the writer refuses are
        dropped (see the module docstring) instead of raising ``ValidationRejected``."""
        total = WriteResult()
        ledger = _MintLedger()
        for batch in chunks(claims, batch_size):
            if drop_rejected:
                total += self._write_dropping(layer, list(batch), worker, run_id, ledger)
            else:
                total += self.write_batch(layer, batch, worker=worker, run_id=run_id)
        return total

    def _write_dropping(
        self, layer: str, pending: list[ClaimWrite], worker: str, run_id: int | None, ledger: _MintLedger
    ) -> WriteResult:
        """One batch: send, drop what the writer refuses (and its dependents), re-send the rest.

        Every round removes at least one claim, so this ends after at most ``len(pending)``
        rejections."""
        out = WriteResult()
        while True:
            pending = self._drop_dependents(pending, ledger, out)
            if not pending:
                return out
            try:
                res = self.write_batch(layer, pending, worker=worker, run_id=run_id)
            except ValidationRejected as e:
                reasons = _per_claim(e, len(pending))
                if reasons is None:
                    raise  # not about specific claims: the run cannot go on
                for i in sorted(reasons):
                    self._reject(out, pending[i], "; ".join(reasons[i]))
                    ledger.refused |= minted_ids(pending[i])
                pending = [c for i, c in enumerate(pending) if i not in reasons]
                continue
            out.written += res.written
            out.minted += res.minted
            for c in pending:
                ledger.written |= minted_ids(c)
            return out

    def _drop_dependents(
        self, pending: list[ClaimWrite], ledger: _MintLedger, out: WriteResult
    ) -> list[ClaimWrite]:
        """Drop claims whose ``{"id"}`` refs name an entity that only refused claims would have
        minted (not written earlier in this call, not minted by a claim still pending); repeated
        until nothing changes, since a dropped claim may have been another's minter."""
        while True:
            minted_here = set().union(*(minted_ids(c) for c in pending)) if pending else set()
            lost = ledger.refused - ledger.written - minted_here
            keep: list[ClaimWrite] = []
            for c in pending:
                missing = sorted(_id_refs(c) & lost)
                if missing:
                    self._reject(out, c, f"depends on rejected mint {', '.join(missing)}")
                    ledger.refused |= minted_ids(c)
                else:
                    keep.append(c)
            if len(keep) == len(pending):
                return keep
            pending = keep

    @staticmethod
    def _reject(out: WriteResult, claim: ClaimWrite, reason: str) -> None:
        out.rejected.append(RejectedWrite(claim, reason))
        value = repr(claim.value)
        log.warning(
            "writer rejected claim %s (%s %s = %s): %s",
            claim.source_ref,
            claim.subject_id,
            claim.predicate,
            value if len(value) <= 200 else value[:200] + "...",
            reason,
        )

    def write_batch(
        self, layer: str, claims: Sequence[ClaimWrite], *, worker: str, run_id: int | None = None
    ) -> WriteResult:
        if not claims:
            return WriteResult()
        body: dict[str, Any] = {"layer": layer, "worker": worker, "claims": [c.to_request() for c in claims]}
        if run_id is not None:
            body["run_id"] = run_id
        data = self._request("POST", "/internal/claims", body)
        return WriteResult(int(data.get("written", 0)), int(data.get("minted", 0)))

    # ── workers and runs (CONTRACTS §12) ──

    def register_worker(
        self,
        *,
        name: str,
        kind: str,
        source: str,
        layers: Sequence[str],
        config: dict[str, Any] | None = None,
        description: str | None = None,
        id: str | None = None,
        trust: float | None = None,
    ) -> str:
        """Register a worker and return its id (``POST /internal/workers``).

        Idempotent on ``(name, kind, source)``: returns the existing worker's id, or a new one.
        Re-registering may add ``layers``; ``config`` and ``description`` only apply to a new worker.

        ``id``: register under this exact id (``<name>-<kind>-<6 digits>``, e.g. an imported
        worker keeping its id); the existing worker with that id is returned if there is one.

        ``trust``: the trust this worker should have per the source trust table
        (``config/trust.yaml``, see ``identity.trust_for``). The API gives a new worker its kind
        default, so the value is applied afterwards with ``PATCH /v0/admin/workers/:id`` -- only
        while the worker's trust is still table-managed: equal to its kind default, or equal to
        what the table set earlier (``config.trust_source == 'table'`` and ``config.trust_table``).
        A trust an admin changed is never touched. Applying needs an admin token; without one a
        one-line hint is logged and registration continues. Reading or patching trust never fails
        the registration. A new worker's ``config`` records ``trust_source``/``trust_table`` when
        the table value differs from the kind default; applying the table to an existing worker
        writes the same marker (``PATCH`` merges ``config`` shallowly)."""
        body: dict[str, Any] = {"name": name, "kind": kind, "source": source, "layers": list(layers)}
        cfg = dict(config or {})
        if trust is not None and kind in DEFAULT_TRUST and not _same_trust(trust, DEFAULT_TRUST[kind]):
            cfg.update(trust_source="table", trust_table=trust)
        if cfg:
            body["config"] = cfg
        if description:
            body["description"] = description
        if id:
            body["id"] = id
        data = self._request("POST", "/internal/workers", body)
        worker_id = str(data["id"])
        if trust is not None:
            self._apply_trust(worker_id, kind, float(trust), created=data.get("created") is True)
        return worker_id

    def _apply_trust(self, worker_id: str, kind: str, trust: float, *, created: bool) -> None:
        """Bring a table-managed worker's trust to ``trust`` (see ``register_worker``)."""
        if worker_id in self._trust_done and _same_trust(self._trust_done[worker_id], trust):
            return
        self._trust_done[worker_id] = trust
        default = DEFAULT_TRUST.get(kind)
        if created:
            current: Any = default
            cfg: dict[str, Any] = {}
        else:
            try:
                w = self._request("GET", f"/v0/workers/{worker_id}")
            except WriterError as e:  # e.g. a worker with only private layers is not listed
                log.debug("worker %s: cannot read its trust (%s); leaving it", worker_id, e)
                self._trust_done.pop(worker_id, None)  # try again on the next registration
                return
            current, cfg = w.get("trust"), w.get("config") or {}
        if current is None or _same_trust(current, trust):
            return
        current = round(float(current), 4)  # a float4 read back may be 0.800000011920929
        managed = (default is not None and _same_trust(current, default)) or (
            cfg.get("trust_source") == "table" and _same_trust(current, cfg.get("trust_table"))
        )
        if not managed:
            log.debug(
                "worker %s: trust %s was set by an admin; table value %s not applied",
                worker_id,
                current,
                trust,
            )
            return
        if not self.admin_token:
            log.info(
                "worker %s: trust is %s, config/trust.yaml says %s; set ADMIN_TOKEN for the worker "
                "to apply it (or PATCH /v0/admin/workers/%s)",
                worker_id,
                current,
                trust,
                worker_id,
            )
            return
        try:
            # The marker keeps the worker table-managed when the table changes again (S19).
            body = {"trust": trust, "config": {"trust_source": "table", "trust_table": trust}}
            self._request("PATCH", f"/v0/admin/workers/{worker_id}", body, admin=True)
        except WriterError as e:
            log.warning("worker %s: could not set trust %s: %s", worker_id, trust, e)
            self._trust_done.pop(worker_id, None)
            return
        log.info("worker %s: trust %s -> %s (config/trust.yaml)", worker_id, current, trust)

    def open_run(self, worker: str, kind: str) -> int:
        return int(self._request("POST", "/internal/runs", {"worker": worker, "kind": kind})["run_id"])

    def update_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        stats: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Merge ``stats`` into the run; ``status`` 'ok'/'failed' closes it."""
        body: dict[str, Any] = {}
        if status is not None:
            body["status"] = status
        if stats:
            body["stats"] = stats
        if error is not None:
            body["error"] = error[:4000]
        return self._request("PATCH", f"/internal/runs/{run_id}", body)

    # ── transport ──

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, admin: bool = False
    ) -> dict[str, Any]:
        """JSON request with retries on network errors, 429 and 5xx. 400 raises ValidationRejected.
        ``admin`` sends the admin token instead of the ingest token."""
        headers = {"Authorization": f"Bearer {self.admin_token}"} if admin and self.admin_token else None
        attempt = 0
        while True:
            try:
                resp = self.http.request(method, path, json=body, headers=headers)
            except httpx.TransportError as e:
                error: str = f"network error: {e!r}"
            else:
                if resp.status_code in (200, 201):
                    return _json(resp)
                if resp.status_code == 400:
                    data = _json(resp)
                    details = data.get("details") or []
                    shown = "; ".join(f"[{d.get('index')}] {d.get('message')}" for d in details[:10])
                    what = "batch" if path == "/internal/claims" else f"{method} {path}"
                    raise ValidationRejected(
                        f"writer rejected {what} ({len(details)} errors): {shown}", details
                    )
                if resp.status_code < 500 and resp.status_code != 429:
                    raise WriterError(f"writer returned {resp.status_code}: {resp.text[:500]}")
                error = f"writer returned {resp.status_code}: {resp.text[:200]}"
            attempt += 1
            if attempt > self.max_retries:
                raise WriterError(f"giving up after {attempt} attempts: {error}")
            delay = min(self.max_backoff_s, self.backoff_s * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
            log.warning(
                "%s %s failed (%s); retry %d/%d in %.1fs",
                method,
                path,
                error,
                attempt,
                self.max_retries,
                delay,
            )
            self.sleep(delay)

    def close(self) -> None:
        self.http.close()


def _per_claim(e: ValidationRejected, n: int) -> dict[int, list[str]] | None:
    """The writer's reasons by claim index, or None when the 400 is not (only) about specific
    claims of this batch (no details, index -1, or an index out of range)."""
    reasons: dict[int, list[str]] = {}
    for d in e.details:
        i = d.get("index") if isinstance(d, dict) else None
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n:
            return None
        reasons.setdefault(i, []).append(str(d.get("message") or "rejected"))
    return reasons or None


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
