"""X profiles (``public/x``): enrichment of entities that already have an ``x_handle``.

Enrichment, not discovery: the handles come from the graph (Wikidata, GitHub, ...), read as
``rebox_app`` with all public layers visible (``DATABASE_URL`` from the worker environment).
Live mode needs ``X_BEARER_TOKEN``; without it ``enabled()`` is False and ``fetch`` refuses to
run. Nobody has provided a token yet, so the connector is built and tested against synthetic
fixtures (``fixtures/raw/x/``, fake handles only) served through an httpx mock transport.

fetch
    Handles (sorted, lowercased) after the cursor, in batches of ``BATCH`` (100, the API max) to
    ``GET https://api.x.com/2/users/by?usernames=...&user.fields=...``. ``entities`` is
    requested in addition to name, description, location, url and public_metrics, because the
    profile ``url`` is a t.co link and only ``entities.url.urls[].expanded_url`` has the real
    site. One RawRecord per found profile (``external_id = user:<handle>``); its payload also
    records which entity kinds carry the handle and whether they already have a name, so that
    ``extract`` stays pure. Cursor = last handle of the last finished batch. Rate limits: 429
    waits until ``x-rate-limit-reset``; ``x-rate-limit-remaining: 0`` pauses before the next
    batch.

extract (pure)
    For each kind carrying the handle: subject = EntityRef(kind, identity x_handle);
    ``x_followers``; ``headline`` (people) or ``description`` (companies); ``location``
    (people); ``website`` (expanded URL, never t.co); ``name`` only if the entity had none.

source_ref: ``x:<handle>:<predicate>:<kind>``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from ..db import Db
from ..models import ClaimInput, CursorUpdate, EntityRef, RawRecord
from .base import Ctx
from .github import token
from .hn import HttpGiveUp, retry_after_s

API = "https://api.x.com/2/users/by"
TOKEN_ENV = "X_BEARER_TOKEN"
BATCH = 100
USER_FIELDS = "name,description,location,url,public_metrics,entities"
MIN_INTERVAL_S = 1.0
MAX_RETRIES = 5
X_MAX_WAIT_S = 16 * 60.0  # X windows are 15 minutes; waiting one out is expected

HANDLES_SQL = """
select c.value_text as handle, e.kind,
       bool_or(exists (select 1 from claim n
                        where n.subject = e.id and n.predicate = 'name'
                          and n.object_id is null and not n.retract)) as named
  from claim c
  join entity e on e.id = c.subject
 where c.predicate = 'x_handle' and c.object_id is null and not c.retract
   and e.kind in ('person', 'company')
 group by c.value_text, e.kind
 order by c.value_text, e.kind
"""


class X:
    name = "x"
    layer = "public/x"
    interval_s = 604800

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_INTERVAL_S,
        handles: Callable[[Ctx], dict[str, list[dict[str, Any]]]] | None = None,
    ) -> None:
        self.transport = transport
        self.sleep = sleep
        self.min_interval_s = min_interval_s
        self.handles = handles or graph_handles  # tests inject a fixed handle list

    def enabled(self, ctx: Ctx) -> bool:
        return token(ctx, TOKEN_ENV) is not None

    # ── fetch: the only part that does I/O ──
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        tok = token(ctx, TOKEN_ENV)
        if tok is None:
            raise RuntimeError(f"x connector is disabled: set {TOKEN_ENV}")
        after = (cursor or {}).get("last_handle") or ""
        graph = {h: kinds for h, kinds in self.handles(ctx).items() if h > after}
        handles = sorted(graph)
        ctx.log.info("x: %d handles in the graph after cursor %r", len(handles), after)
        n = 0
        with ctx.http(transport=self.transport, headers={"Authorization": f"Bearer {tok}"}) as client:
            for i in range(0, len(handles), BATCH):
                batch = handles[i : i + BATCH]
                if ctx.limit is not None:
                    batch = batch[: max(0, ctx.limit - n)]
                    if not batch:
                        return
                data = self._lookup(client, ctx, batch)
                for user in sorted(data.get("data") or [], key=lambda u: str(u.get("username", "")).lower()):
                    handle = str(user.get("username", "")).lower()
                    if handle not in graph:
                        continue
                    n += 1
                    yield RawRecord.new(
                        self.name,
                        f"user:{handle}",
                        {"user": user, "graph": graph[handle]},
                        url=f"https://x.com/{handle}",
                    )
                yield CursorUpdate(cursor={"last_handle": batch[-1]})
        yield CursorUpdate(cursor={"last_handle": None, "last_run_profiles": n})  # pass complete

    def _lookup(self, client: httpx.Client, ctx: Ctx, batch: list[str]) -> dict[str, Any]:
        params = {"usernames": ",".join(batch), "user.fields": USER_FIELDS}
        error = ""
        for attempt in range(MAX_RETRIES + 1):
            self.sleep(self.min_interval_s)
            try:
                resp = client.get(API, params=params)
            except httpx.TransportError as e:
                error, wait = repr(e), None
            else:
                if resp.status_code == 200:
                    if resp.headers.get("x-rate-limit-remaining") == "0":
                        pause = _until_reset(resp)
                        if pause:
                            ctx.log.info("x: rate limit window exhausted; pausing %.0fs", pause)
                            self.sleep(pause)
                    return resp.json()
                if resp.status_code < 500 and resp.status_code != 429:
                    resp.raise_for_status()
                error, wait = f"HTTP {resp.status_code}", retry_after_s(resp)
                if wait is not None and wait > X_MAX_WAIT_S:
                    raise HttpGiveUp(f"x: {error}, server asks to wait {wait:.0f}s; resume next run")
            if attempt == MAX_RETRIES:
                break
            delay = wait if wait is not None else min(60.0, 2.0 * 2**attempt)
            ctx.log.warning("x: %s; retry %d/%d in %.0fs", error, attempt + 1, MAX_RETRIES, delay)
            self.sleep(delay)
        raise HttpGiveUp(f"x: giving up after {MAX_RETRIES + 1} attempts: {error}")

    # ── extract: pure ──
    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        user: dict[str, Any] = rec.payload.get("user") or {}
        handle = str(user.get("username") or "").lower()
        if not handle:
            return []
        out: list[ClaimInput] = []
        followers = (user.get("public_metrics") or {}).get("followers_count")
        description = _clean(user.get("description"))
        location = _clean(user.get("location"))
        website = expanded_url(user)
        name = _clean(user.get("name"))
        for g in sorted(rec.payload.get("graph") or [], key=lambda g: g["kind"]):
            kind = g["kind"]
            subject = EntityRef(kind=kind, identity={"x_handle": handle})

            def add(predicate: str, value: Any, kind: str = kind, subject: EntityRef = subject) -> None:
                out.append(
                    ClaimInput(
                        subject=subject,
                        predicate=predicate,
                        value=value,
                        source_ref=f"{self.name}:{handle}:{predicate}:{kind}",
                    )
                )

            if name and not g.get("named"):
                add("name", name)
            if isinstance(followers, int) and followers >= 0:
                add("x_followers", followers)
            if description:
                add("headline" if kind == "person" else "description", description)
            if location and kind == "person":
                add("location", location)
            if website:
                add("website", website)
        return out


def graph_handles(ctx: Ctx) -> dict[str, list[dict[str, Any]]]:
    """handle -> [{"kind", "named"}] for every x_handle in the public graph."""
    url = ctx.env.get("DATABASE_URL")
    if not url:
        raise RuntimeError("x connector needs DATABASE_URL to read the handles already in the graph")
    out: dict[str, list[dict[str, Any]]] = {}
    with Db(url, max_size=1) as db, db.view() as conn:
        for row in conn.execute(HANDLES_SQL):
            out.setdefault(row["handle"], []).append({"kind": row["kind"], "named": bool(row["named"])})
    return out


def expanded_url(user: dict[str, Any]) -> str | None:
    """The profile website: ``entities.url.urls[].expanded_url`` matching ``url``; never t.co."""
    urls = ((user.get("entities") or {}).get("url") or {}).get("urls") or []
    short = user.get("url")
    for u in urls:
        if not isinstance(u, dict):
            continue
        exp = u.get("expanded_url")
        if exp and (not short or u.get("url") == short) and not _is_tco(exp):
            return exp
    if isinstance(short, str) and short and not _is_tco(short):
        return short
    return None


def _until_reset(resp: httpx.Response) -> float | None:
    try:
        return min(X_MAX_WAIT_S, max(0.0, float(resp.headers["x-rate-limit-reset"]) - time.time()) + 1.0)
    except (KeyError, ValueError):
        return None


def _is_tco(u: str) -> bool:
    return u.lower().split("://", 1)[-1].startswith("t.co/")


def _clean(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s or None
