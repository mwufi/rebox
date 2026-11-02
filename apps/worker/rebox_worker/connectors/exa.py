"""Exa discovery (``public/exa``): fresh SF startup activity found by Exa web search.

Queries come from ``config/seeds.yaml`` -> ``exa.queries``: each is a string or
``{q, category?, days?, num?, include_domains?}``. Needs ``EXA_API_KEY``. Budget:
``EXA_MAX_REQUESTS_PER_RUN`` (default 100) search requests per run; when there are more queries
than that, the cursor remembers where the last run stopped (round robin).

fetch
    ``POST https://api.exa.ai/search`` with ``numResults`` <= 25, ``startPublishedDate`` =
    now - ``days`` (default 30; named-company queries with ``include_domains`` have no date
    filter), and contents (text capped at ``TEXT_CHARS`` characters, highlights, summary). One
    ``exa_result`` RawRecord per result (``external_id = url:<normalized url>``; the same URL
    from two queries is one record, its ``queries`` list grows); payload:
    ``{type, query, category, url, title, published_date, author, text, highlights, summary,
    image, favicon, homepage}``. Company homepages are upserted into ``frontier``
    (``source='exa'``, ``key`` = domain, ``score`` = number of distinct queries that found it).
    The cost Exa reports (``costDollars.total``) is summed into ``run_stats.cost_usd``.

extract (pure)
    Only company homepages (``homepage = true``: the URL is the root (or a language root) of a
    non-platform, non-news host) produce claims: a ``company`` with identity ``domain``,
    ``name`` (the site title cleaned to the segment that matches the host, e.g.
    "Ditto — The first AI social agent" -> "Ditto"), ``website`` (the site root) and a
    ``description`` from the page's meta summary when Exa gives one. Articles (news, launch
    posts, funding announcements) produce nothing here: the parser slice reads them.

source_ref: ``exa:domain:<domain>:<predicate>``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import frontier
from ..models import ClaimInput, CursorUpdate, EntityRef, RawRecord
from ..normalize import url as normalize_url
from .base import Ctx
from .hn import _BATCH_RE, PLATFORM_HOSTS, HttpGiveUp, name_matches_host, retry_after_s, site_domain

API = "https://api.exa.ai/search"
TOKEN_ENV = "EXA_API_KEY"
DEFAULT_REQUESTS = 100
DEFAULT_DAYS = 30
MAX_RESULTS = 25
TEXT_CHARS = 3000
MIN_INTERVAL_S = 0.5

# News, aggregators and directories: their pages are articles about companies, never homepages.
NEWS_HOSTS = frozenset(
    {
        "techcrunch.com", "venturebeat.com", "wired.com", "nytimes.com", "bloomberg.com", "forbes.com",
        "businessinsider.com", "theverge.com", "wsj.com", "cnbc.com", "reuters.com", "axios.com",
        "theinformation.com", "fortune.com", "sfchronicle.com", "sfstandard.com", "sfgate.com",
        "bizjournals.com", "geekwire.com", "siliconangle.com", "finsmes.com", "prnewswire.com",
        "businesswire.com", "globenewswire.com", "crunchbase.com", "news.crunchbase.com",
        "pitchbook.com", "tracxn.com", "cbinsights.com", "ycombinator.com", "producthunt.com",
        "linkedin.com", "wellfound.com", "angel.co", "f6s.com", "seedtable.com", "vcbacked.co",
        "startuphub.ai", "tynmagazine.com", "fundediq.co", "simplify.jobs", "builtin.com",
        "builtinsf.com", "glassdoor.com", "indeed.com", "lu.ma", "luma.com", "eventbrite.com",
        "meetup.com", "x.com", "twitter.com", "youtube.com", "medium.com", "substack.com",
        "techmeme.com", "hackernoon.com", "zdnet.com", "engadget.com", "arstechnica.com",
        "pulse.bot", "aiweekly.co", "digg.com", "alleywatch.com", "inc.com", "fastcompany.com",
    }
)  # fmt: skip
LANG_ROOTS = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?|home|index\.html?)?/?$", re.I)
_YC_TAG_RE = re.compile(r"\s*\((?:YC|Y Combinator)\s+[WSFXP]\d{2}\)", re.I)
# Exa categories with dedicated indices that reject date filters (HTTP 400).
UNDATED_CATEGORIES = frozenset({"company", "people", "linkedin profile"})
TITLE_SPLIT = re.compile(r"\s+[|–—\-:·•]\s+|\s*[|–—·•]\s*")


class Exa:
    name = "exa"
    layer = "public/exa"
    interval_s = 86400
    scheduled = True
    worker_kind = "spider"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_INTERVAL_S,
        queries: list[frontier.ExaQuery] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.transport = transport
        self.sleep = sleep
        self.min_interval_s = min_interval_s
        self._queries = queries
        self.now = now
        self.run_stats: dict[str, Any] = {}

    def enabled(self, ctx: Ctx) -> bool:
        return bool((ctx.env.get(TOKEN_ENV) or "").strip())

    def workers(self, ctx: Ctx | None = None) -> list[dict[str, Any]]:
        return [
            {
                "name": "exa",
                "kind": self.worker_kind,
                "source": self.name,
                "layers": [self.layer],
                "config": {"queries": len(self.queries(ctx))},
                "description": "Exa search for fresh SF startup launches, rounds and named startups",
            }
        ]

    def queries(self, ctx: Ctx | None = None) -> list[frontier.ExaQuery]:
        if self._queries is not None:
            return self._queries
        return frontier.load_seeds(ctx.env if ctx is not None else None).exa_queries

    # ── fetch ──
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        key = (ctx.env.get(TOKEN_ENV) or "").strip()
        if not key:
            raise RuntimeError(f"exa connector is disabled: set {TOKEN_ENV}")
        queries = self.queries(ctx)
        budget = int(_num(ctx.env.get("EXA_MAX_REQUESTS_PER_RUN"), DEFAULT_REQUESTS))
        start = int((cursor or {}).get("next_query") or 0) % max(1, len(queries) or 1)
        order = [*range(start, len(queries)), *range(0, start)][: max(0, budget)]
        stats = {"api_calls": 0, "cost_usd": 0.0, "results": 0, "homepages": 0, "queries": len(order)}
        self.run_stats = stats
        seen: set[str] = set()
        n = 0
        with ctx.http(transport=self.transport, headers={"x-api-key": key}) as client:
            for done, qi in enumerate(order, start=1):
                q = queries[qi]
                stats["api_calls"] += 1
                try:
                    data = post_json(
                        client,
                        API,
                        self._body(q),
                        log=ctx.log,
                        sleep=self.sleep,
                        min_interval_s=self.min_interval_s,
                    )
                except ExaBadRequest as e:  # one bad query must not sink the run
                    ctx.log.warning("exa: query %r rejected: %s", q.q, e)
                    stats.setdefault("failed_queries", []).append(q.q)
                    data = {}
                stats["cost_usd"] = round(stats["cost_usd"] + _cost(data), 5)
                items: list[frontier.Item] = []
                records: list[RawRecord] = []
                for r in data.get("results") or []:
                    payload = result_payload(r, q)
                    if payload is None or ctx.limit is not None and n >= ctx.limit:
                        continue
                    if payload["homepage"]:
                        items.append(
                            frontier.Item(
                                key=payload["domain"],
                                kind="company",
                                label=homepage_name(payload) or payload["domain"],
                                discovered_by=[f"exa:{q.q}"],
                                payload={"url": payload["url"], "title": payload.get("title")},
                            )
                        )
                    ext = f"url:{payload['url_key']}"
                    if ext in seen:  # the same page from an earlier query of this run
                        continue
                    seen.add(ext)
                    n += 1
                    stats["results"] += 1
                    stats["homepages"] += int(payload["homepage"])
                    records.append(RawRecord.new(self.name, ext, payload, url=payload["url"]))
                if items and ctx.db is not None:
                    with ctx.db.view() as conn:
                        frontier.upsert(conn, self.name, items)
                yield from records
                nxt = (qi + 1) % max(1, len(queries))
                yield CursorUpdate(cursor={"next_query": nxt, "last_run": {**stats, "queries_done": done}})
                if ctx.limit is not None and n >= ctx.limit:
                    break
        ctx.log.info("exa: run stats %s", stats)

    def _body(self, q: frontier.ExaQuery) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": q.q,
            "type": "auto",
            "numResults": max(1, min(MAX_RESULTS, q.num)),
            "contents": {
                "text": {"maxCharacters": TEXT_CHARS},
                "highlights": {"numSentences": 3, "highlightsPerUrl": 3},
                "summary": {},
            },
        }
        if q.category:
            body["category"] = q.category
        if q.include_domains:
            body["includeDomains"] = list(q.include_domains)
        days = q.days if q.days is not None else (None if q.include_domains else DEFAULT_DAYS)
        if days and q.category not in UNDATED_CATEGORIES:
            since = self.now() - timedelta(days=days)
            body["startPublishedDate"] = since.strftime("%Y-%m-%dT00:00:00.000Z")
        return body

    # ── extract (pure) ──
    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        p = rec.payload if isinstance(rec.payload, dict) else {}
        if p.get("type") != "exa_result" or not p.get("homepage") or not p.get("domain"):
            return []
        dom = str(p["domain"])
        name = homepage_name(p)
        if not name:
            return []
        subject = EntityRef(kind="company", identity={"domain": dom}, name=name)
        out: list[ClaimInput] = []

        def add(predicate: str, value: Any) -> None:
            if value:
                out.append(
                    ClaimInput(
                        subject=subject,
                        predicate=predicate,
                        value=value,
                        source_ref=f"exa:domain:{dom}:{predicate}",
                    )
                )

        add("domain", dom)
        add("name", name)
        add("website", f"https://{dom}")
        add("yc_batch", homepage_batch(p))
        return out


def result_payload(r: dict[str, Any], q: frontier.ExaQuery) -> dict[str, Any] | None:
    u = r.get("url")
    if not isinstance(u, str) or not u.startswith(("http://", "https://")):
        return None
    try:
        key = normalize_url(u)
    except ValueError:
        return None
    dom = site_domain(u)
    path = urlsplit(u).path or "/"
    homepage = bool(
        dom
        and dom not in NEWS_HOSTS
        and not any(dom.endswith("." + h) for h in NEWS_HOSTS)
        and dom not in PLATFORM_HOSTS
        and LANG_ROOTS.match(path)
    )
    return {
        "type": "exa_result",
        "query": q.q,
        "category": q.category,
        "url": u,
        "url_key": key,
        "domain": dom,
        "homepage": homepage,
        "title": _clean(r.get("title")),
        "published_date": r.get("publishedDate"),
        "author": _clean(r.get("author")),
        "text": r.get("text") if isinstance(r.get("text"), str) else None,
        "highlights": [h for h in r.get("highlights") or [] if isinstance(h, str)],
        "summary": _clean(r.get("summary")),
        "image": r.get("image") if isinstance(r.get("image"), str) else None,
        "favicon": r.get("favicon") if isinstance(r.get("favicon"), str) else None,
    }


def homepage_batch(p: dict[str, Any]) -> str | None:
    """``Kebra (YC S26)`` -> ``S26``."""
    m = _BATCH_RE.search(_clean(p.get("title")) or "")
    return f"{m.group('b').upper()}{m.group('y')}" if m else None


def homepage_name(p: dict[str, Any]) -> str | None:
    """The company name from a homepage title: the title segment that matches the host
    ("Home | Acme AI" on acme.ai -> "Acme AI"), without a "(YC S26)" tag; else the host's label
    capitalized."""
    dom = str(p.get("domain") or "")
    title = _YC_TAG_RE.sub(" ", _clean(p.get("title")) or "")
    segments = [s.strip() for s in TITLE_SPLIT.split(title) if s and s.strip()]
    for s in segments:
        if 1 <= len(s) <= 40 and dom and name_matches_host(s, dom):
            return s
    for s in segments:  # "Ditto" style: a short first word matching the host label
        first = s.split()[0] if s.split() else ""
        if first and dom and name_matches_host(first, dom):
            return first
    label_key = re.sub(r"[^a-z0-9]", "", dom.split(".")[0])
    for s in segments:  # "Known" on knowndating.com: the host starts with the brand
        first = s.split()[0] if s.split() else ""
        fk = re.sub(r"[^a-z0-9]", "", first.lower())
        if len(fk) >= 4 and label_key.startswith(fk):
            return first.rstrip(":,.")
    if not dom:
        return None
    label = dom.split(".")[0]
    return label[:1].upper() + label[1:] if len(label) >= 2 else None


class ExaBadRequest(HttpGiveUp):
    """400: this query is invalid (e.g. a filter the category does not support)."""


def post_json(
    client: httpx.Client,
    url: str,
    body: dict[str, Any],
    *,
    log: Any,
    sleep: Callable[[float], None],
    min_interval_s: float,
    max_retries: int = 3,
) -> dict[str, Any]:
    """POST JSON -> JSON, retrying 429/5xx/network errors (honouring Retry-After) with backoff.
    Other 4xx raise immediately (a bad query or key is not fixed by retrying)."""
    error = ""
    for attempt in range(max_retries + 1):
        sleep(min_interval_s)
        try:
            resp = client.post(url, json=body)
        except httpx.TransportError as e:
            error, wait = repr(e), None
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 400:
                raise ExaBadRequest(f"exa: HTTP 400: {resp.text[:300]}")
            if resp.status_code < 500 and resp.status_code != 429:
                raise HttpGiveUp(f"exa: HTTP {resp.status_code}: {resp.text[:300]}")
            error, wait = f"HTTP {resp.status_code}", retry_after_s(resp)
        if attempt == max_retries:
            break
        delay = wait if wait is not None else min(30.0, 2.0 * 2**attempt)
        log.warning("exa: %s; retry %d/%d in %.0fs", error, attempt + 1, max_retries, delay)
        sleep(delay)
    raise HttpGiveUp(f"exa: giving up after {max_retries + 1} attempts: {error}")


def _cost(data: dict[str, Any]) -> float:
    c = data.get("costDollars")
    if isinstance(c, dict):
        try:
            return float(c.get("total") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _num(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _clean(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s or None
