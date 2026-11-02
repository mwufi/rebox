"""The Connector protocol (CONTRACTS §8) and the context ``fetch`` receives.

A connector is one file in this package with one class:

    class Hn:
        name = "hn"
        layer = "public/hn"
        interval_s = 3600

        def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
            ...  # the only code that touches the network; use ctx.http() and ctx.limit

        def extract(self, rec: RawRecord) -> list[ClaimInput]:
            ...  # pure: RawRecord -> claims, no network, no DB

Register it with one line in ``connectors/__init__.py``. Optional: ``enabled(ctx) -> bool``
(e.g. False without an API token; the scheduler then skips it), and ``scheduled = False`` to
keep a connector out of the automatic schedule (it then runs only on ``sync``/``enqueue``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx

from ..models import ClaimInput, CursorUpdate, RawRecord

if TYPE_CHECKING:
    from ..db import Db


@runtime_checkable
class Connector(Protocol):
    name: str
    layer: str
    interval_s: int

    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]: ...

    def extract(self, rec: RawRecord) -> list[ClaimInput]: ...


def is_enabled(connector: Connector, ctx: Ctx) -> bool:
    check = getattr(connector, "enabled", None)
    return bool(check(ctx)) if callable(check) else True


def is_scheduled(connector: Connector) -> bool:
    """False for connectors that only run on demand (e.g. ``example``, which is test data)."""
    return bool(getattr(connector, "scheduled", True))


def fixtures_root(environ: Mapping[str, str] | None = None) -> Path:
    """Directory holding ``<source>/`` fixture folders (CONTRACTS §11).

    ``$FIXTURES_DIR`` if set (either the ``fixtures/raw`` dir itself or the ``fixtures`` dir
    above it); otherwise ``fixtures/raw`` in the nearest ancestor of this package that has a
    ``fixtures/`` directory (the repo root in a checkout, ``/app`` in the image).
    """
    env = os.environ if environ is None else environ
    if env.get("FIXTURES_DIR"):
        root = Path(env["FIXTURES_DIR"]).expanduser().resolve()
        return root / "raw" if (root / "raw").is_dir() and root.name != "raw" else root
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "fixtures").is_dir():
            return parent / "fixtures" / "raw"
    return Path.cwd() / "fixtures" / "raw"


@dataclass
class Ctx:
    """What a connector's ``fetch`` (and ``enabled``) may use. Built by the CLI from Settings.

    ``env`` is the worker's environment (tokens such as GITHUB_TOKEN, X_BEARER_TOKEN); the CLI and
    the scheduler pass ``os.environ``. ``db`` is the worker's own pool (role ``rebox_app``, use
    ``db.view()``), for connectors that read the graph (X enriches handles already in it); the
    pipeline fills it in, so connectors never open their own connection.
    """

    user_agent: str = "rebox-bot/0.1"
    limit: int | None = None  # stop after this many raw records (dev runs)
    env: Mapping[str, str] = field(default_factory=dict)  # e.g. GITHUB_TOKEN, X_BEARER_TOKEN
    db: Db | None = None
    fixtures: Path = field(default_factory=fixtures_root)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("rebox_worker.connector"))

    def http(self, **kwargs: object) -> httpx.Client:
        """An httpx client with the bot User-Agent (system proxy/CA settings are honoured)."""
        headers = {"User-Agent": self.user_agent, **dict(kwargs.pop("headers", {}) or {})}  # type: ignore[arg-type]
        return httpx.Client(headers=headers, timeout=kwargs.pop("timeout", 30.0), **kwargs)  # type: ignore[arg-type]

    def fixtures_for(self, source: str) -> Path:
        return self.fixtures / source
