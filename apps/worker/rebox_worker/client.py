"""Writer API client (CONTRACTS §5, §12).

``POST /internal/claims`` (every batch names its ``worker`` and ``run_id``), ``POST /internal/workers``
(idempotent registration), ``POST /internal/runs`` and ``PATCH /internal/runs/:id``.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from .models import ClaimWrite

log = logging.getLogger(__name__)

BATCH_SIZE = 500  # API max is 1000 per request


class WriterError(RuntimeError):
    pass


class ValidationRejected(WriterError):
    """400 from the writer: nothing in the batch was written."""

    def __init__(self, message: str, details: list[dict[str, Any]]):
        super().__init__(message)
        self.details = details


@dataclass
class WriteResult:
    written: int = 0
    minted: int = 0

    def __iadd__(self, other: WriteResult) -> WriteResult:
        self.written += other.written
        self.minted += other.minted
        return self


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
    ) -> None:
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
    ) -> WriteResult:
        """Write ``claims`` as ``worker`` (in ``run_id``) in batches of at most ``batch_size``;
        each batch is one transaction."""
        total = WriteResult()
        for batch in chunks(claims, batch_size):
            total += self.write_batch(layer, batch, worker=worker, run_id=run_id)
        return total

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
    ) -> str:
        """Idempotent on (name, kind, source): returns the existing worker's id, or a new one."""
        body: dict[str, Any] = {"name": name, "kind": kind, "source": source, "layers": list(layers)}
        if config:
            body["config"] = config
        if description:
            body["description"] = description
        return str(self._request("POST", "/internal/workers", body)["id"])

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

    def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """JSON request with retries on network errors, 429 and 5xx. 400 raises ValidationRejected."""
        attempt = 0
        while True:
            try:
                resp = self.http.request(method, path, json=body)
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


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
