"""Connector tests run offline: any real network access fails the test.

Fetch tests use ``httpx.MockTransport`` serving recorded (Wikidata, HN) or synthetic (GitHub,
X) fixtures from ``fixtures/raw/<source>/``.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from rebox_worker.connectors.base import Ctx
from rebox_worker.models import RawRecord

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "raw"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block outbound connections (localhost stays allowed for the DB and the fake writer).

    Outbound HTTPS in this environment goes through a proxy on localhost, so the proxy
    variables are cleared too: a request to a real host then fails at DNS / connect.
    """
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    real = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost") and "/" not in host:
            raise RuntimeError(f"network access in tests: {address!r}")
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "getaddrinfo", _local_only(socket.getaddrinfo))


def _local_only(real: Any) -> Any:
    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in (None, "127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"DNS lookup in tests: {host!r}")
        return real(host, *args, **kwargs)

    return getaddrinfo


def records(source: str) -> list[RawRecord]:
    return [
        RawRecord.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted((FIXTURES / source).glob("*.json"))
    ]


def record(source: str, external_id: str) -> RawRecord:
    for r in records(source):
        if r.external_id == external_id:
            return r
    raise KeyError(external_id)


def fixture_json(path: str) -> Any:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def ctx(**kw: Any) -> Ctx:
    return Ctx(user_agent="rebox-bot/0.1 (+https://github.com/rebox)", fixtures=FIXTURES, **kw)


def facts(claims: list) -> set[tuple]:
    """(subject kind, subject name, predicate, value or object name) for compact asserts."""
    out = set()
    for c in claims:
        obj = c.object.name if c.object is not None else None
        val = json.dumps(c.value, sort_keys=True) if isinstance(c.value, dict) else c.value
        out.add((c.subject.kind, c.subject.name, c.predicate, obj if obj is not None else val))
    return out
