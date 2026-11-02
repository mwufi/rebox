"""Claims writer client: ``POST {API_URL}/internal/claims`` (CONTRACTS §5)."""

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

    def write(self, layer: str, claims: Sequence[ClaimWrite], batch_size: int = BATCH_SIZE) -> WriteResult:
        """Write ``claims`` in batches of at most ``batch_size``; each batch is one transaction."""
        total = WriteResult()
        for batch in chunks(claims, batch_size):
            total += self.write_batch(layer, batch)
        return total

    def write_batch(self, layer: str, claims: Sequence[ClaimWrite]) -> WriteResult:
        if not claims:
            return WriteResult()
        body = {"layer": layer, "claims": [c.to_request() for c in claims]}
        attempt = 0
        while True:
            try:
                resp = self.http.post("/internal/claims", json=body)
            except httpx.TransportError as e:
                error: str = f"network error: {e!r}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    return WriteResult(int(data.get("written", 0)), int(data.get("minted", 0)))
                if resp.status_code == 400:
                    data = _json(resp)
                    details = data.get("details") or []
                    shown = "; ".join(f"[{d.get('index')}] {d.get('message')}" for d in details[:10])
                    raise ValidationRejected(
                        f"writer rejected batch ({len(details)} errors): {shown}", details
                    )
                if resp.status_code < 500 and resp.status_code != 429:
                    raise WriterError(f"writer returned {resp.status_code}: {resp.text[:500]}")
                error = f"writer returned {resp.status_code}: {resp.text[:200]}"
            attempt += 1
            if attempt > self.max_retries:
                raise WriterError(f"giving up after {attempt} attempts: {error}")
            delay = min(self.max_backoff_s, self.backoff_s * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
            log.warning(
                "claims write failed (%s); retry %d/%d in %.1fs", error, attempt, self.max_retries, delay
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
