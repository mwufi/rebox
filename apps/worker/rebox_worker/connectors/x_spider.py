"""X following-graph spider (``public/x_spider``): who the best-connected SF people follow.

Seeds come from ``config/seeds.yaml`` (``seeds: {<set>: [<username>, ...]}``); each seed set is
its own worker identity (``<set>-spider-<6 digits>``, see ``workers()``). Needs
``X_BEARER_TOKEN``. X is pay-per-use, so every call goes through ``xapi.XBudget``: per-run caps
(``X_MAX_USER_READS_PER_RUN`` / ``X_MAX_POST_READS_PER_RUN``) and a persistent total
(``X_TOTAL_BUDGET_USD``, default 30; estimated spend is kept in ``connector_state.cursor.spend``
and written after every call). The spider stops cleanly when the next page would not fit, and on
402 / 429 / 401 / credit errors (recorded as ``x_stop`` in ``run_stats`` and the cursor).

fetch, in priority order (each phase only when the previous one is complete):

1. **Seeds.** Usernames -> user objects (``GET /2/users/by``), ids cached in the cursor.
2. **Direct following of every seed** (``GET /2/users/:id/following``, ``max_results`` =
   ``X_SPIDER_PAGE_SIZE`` (default 1000) shrunk to the remaining budget), one page per seed in
   turn (round robin), resumable through the stored ``pagination_token``. Each page becomes one
   ``x_following`` RawRecord (the seed's and the followed accounts' user objects, as returned)
   plus one ``x_user`` RawRecord per account; every account is upserted into ``frontier``
   (``key`` = X user id, ``discovered_by`` += seed username, ``score`` = number of distinct
   seeds following it, ``depth`` = seed depth + 1).
3. **Posts** (only once every seed's following is complete): up to 20 recent original posts
   (``GET /2/users/:id/tweets``, ``exclude=replies,retweets``) of the top-scored frontier
   accounts whose kind is in ``X_SPIDER_POST_KINDS`` (default ``company``), one ``x_post``
   RawRecord each.
4. **Depth-1 expansion** (only once every seed's following is complete): the top frontier
   people at depth 1 that look like SF people (location/bio mentions SF or the Bay Area, or no
   location and followed by >= 2 seeds) are crawled like seeds, until the budget is spent;
   finished ones are marked ``fetched`` in the frontier.

The cursor is saved after every page (``CursorUpdate``), so a run that runs out of budget is
continued by the next one.

extract (pure)
    ``x_user`` -> the account's profile facts; ``x_following`` -> ``follows`` edges
    seed -> account; ``x_post`` -> nothing (the AI parser slice reads posts).
    Kind: ``company`` when ``verified_type`` is business/government or the profile has clear
    company signals (``classify``), else ``person``. Facts: ``x_id`` + ``x_handle``
    (identities; resolution matches either), ``name``, ``headline`` (people) /
    ``description`` (companies) with t.co links expanded, ``location`` (people), ``website``
    (``entities.url`` expanded, never t.co; for companies only when the site is the company's
    own), ``avatar`` (400x400), ``x_followers``; companies also get ``domain`` from their own
    website. Mentions such as ``@acme`` in a bio are left for the parser.

source_ref: ``x_spider:user:<x id>:<predicate>``; ``x_spider:follows:<seed id>:<x id>``.
"""

from __future__ import annotations

import copy
import re
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from .. import frontier
from ..models import ClaimInput, CursorUpdate, EntityRef, RawRecord
from ..xapi import (
    MIN_INTERVAL_S,
    TOKEN_ENV,
    BudgetExhausted,
    Spend,
    XAccountError,
    XApi,
    XBudget,
    XStop,
    token,
)
from .base import Ctx
from .hn import _HOST_PREFIXES, _NAME_SUFFIXES, _key, name_matches_host, site_domain

SOURCE = "x_spider"
PHASES = ("following", "posts", "expansion")
PAGE_SIZE = 1000
POSTS_PER_ACCOUNT = 20
EXPANSION_CANDIDATES = 200

# Link-in-bio and similar hosts: never a company's own website.
LINK_HOSTS = frozenset(
    {
        "linktr.ee",
        "beacons.ai",
        "bio.link",
        "lu.ma",
        "luma.com",
        "bento.me",
        "carrd.co",
        "about.me",
        "linkin.bio",
        "msha.ke",
        "campsite.bio",
        "solo.to",
        "taplink.cc",
        "hey.xyz",
        "warpcast.com",
        "bsky.app",
        "threads.net",
        "mastodon.social",
        "instagram.com",
        "youtube.com",
        "tiktok.com",
        "substack.com",
        "medium.com",
        "github.com",
        "linkedin.com",
        "x.com",
        "twitter.com",
    }
)

SF_RE = re.compile(
    r"(?:\bsan\s*francisco\b|\bsf\b|\bs\.f\.|\bbay\s*area\b|\bsoma\b|\bthe\s+mission\b|🌉|"
    r"\bsilicon\s+valley\b|\bpalo\s+alto\b|\bberkeley\b|\boakland\b|\bmenlo\s+park\b|\bmountain\s+view\b)",
    re.I,
)
_OFFICIAL_RE = re.compile(r"\bofficial\s+(?:account|twitter|x\s+account|page|handle)\b", re.I)
_COMPANY_NAME_RE = re.compile(
    r"\b(?:inc|labs?|ai|hq|capital|ventures|vc|fund|partners|technologies|studios?|robotics|"
    r"bio|health|bank|systems|foundation|institute|university|team|app|co|corp|company|"
    r"combinator|research|network)\.?$",
    re.I,
)
_WE_RE = re.compile(r"(?:^|[\s.!])(?:we(?:'re|’re| are| build| make| help)?|our|join us)\b", re.I)
_COMPANY_BIO_RE = re.compile(
    r"\b(?:backed by|\(yc [swfx]\d{2}\)|yc [swfx]\d{2}\b|the platform for|is the|is an?\s|"
    r"we're hiring|now hiring|try it|sign up|download|get started|customer support)\b",
    re.I,
)
_PERSON_BIO_RE = re.compile(
    r"(?:\bi(?:'m|’m| am)?\b|\bmy\b|\bme\b|\bhe/him\b|\bshe/her\b|\bthey/them\b|\bhusband\b|\bwife\b|"
    r"\bdad\b|\bmom\b|\bfather\b|\bmother\b|\bviews (?:are )?my own\b|\bco-?founder\b|\bfounder\b|"
    r"\bceo\b|\bcto\b|\bcoo\b|\bengineer\b|\binvestor\b|\bpartner (?:at|@)|\bgp\b|\bprev\b|\bex-|"
    r"\bformerly\b|\bphd\b|\bresearcher\b|\bdesigner\b|\bbuilding\b|\bangel\b)",
    re.I,
)


class XSpider:
    name = SOURCE
    layer = "public/x_spider"
    interval_s = 86400
    scheduled = False  # paid API: runs only when asked (`sync x_spider` / `enqueue x_spider`)
    worker_kind = "spider"

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_INTERVAL_S,
        seeds: Mapping[str, list[str]] | None = None,
    ) -> None:
        self.transport = transport
        self.sleep = sleep
        self.min_interval_s = min_interval_s
        self._seeds = dict(seeds) if seeds is not None else None
        self.run_stats: dict[str, Any] = {}  # api_calls, cost_usd, ... (merged into the run's stats)

    def enabled(self, ctx: Ctx) -> bool:
        return token(ctx.env) is not None

    def seed_sets(self, ctx: Ctx | None = None) -> dict[str, list[str]]:
        if self._seeds is not None:
            return self._seeds
        return frontier.load_seeds(ctx.env if ctx is not None else None).seeds

    def workers(self, ctx: Ctx | None = None) -> list[dict[str, Any]]:
        """One worker identity per seed set (CONTRACTS §12): ``<set>-spider-<6 digits>``."""
        return [
            {
                "name": name,
                "kind": self.worker_kind,
                "source": self.name,
                "layers": [self.layer],
                "config": {"seeds": handles},
                "description": f"X following-graph spider seeded from {', '.join('@' + h for h in handles)}",
            }
            for name, handles in self.seed_sets(ctx).items()
        ]

    @staticmethod
    def worker_for(rec: RawRecord) -> str | None:
        """The seed set whose worker a record belongs to (None = shared, e.g. an expansion page)."""
        p = rec.payload if isinstance(rec.payload, dict) else {}
        return p.get("seed_set")

    # ── fetch ─────────────────────────────────────────────────────────────
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        tok = token(ctx.env)
        if tok is None:
            raise RuntimeError(f"x_spider is disabled: set {TOKEN_ENV}")
        cur: dict[str, Any] = dict(cursor or {})
        budget = XBudget.from_env(ctx.env, total=Spend.from_dict(cur.get("spend")))
        budget.on_charge = lambda total: _persist_spend(ctx, total)
        page_size = max(1, min(PAGE_SIZE, int(_num(ctx.env.get("X_SPIDER_PAGE_SIZE"), PAGE_SIZE))))
        post_kinds = {
            k.strip() for k in (ctx.env.get("X_SPIDER_POST_KINDS") or "company").split(",") if k.strip()
        }
        phases = {
            p.strip() for p in (ctx.env.get("X_SPIDER_PHASES") or ",".join(PHASES)).split(",") if p.strip()
        }
        self.run_stats = {}
        state = _State(cur, budget)
        sets = self.seed_sets(ctx)
        ctx.log.info(
            "x_spider: seed sets %s; budget: run %d users / %d posts, total $%.2f of $%.2f spent",
            {k: v for k, v in sets.items()},
            budget.run_user_cap,
            budget.run_post_cap,
            budget.total.usd,
            budget.total_usd,
        )
        with ctx.http(transport=self.transport, headers={"Authorization": f"Bearer {tok}"}) as client:
            api = XApi(client, budget, sleep=self.sleep, min_interval_s=self.min_interval_s, log=ctx.log)
            run = _Run(self, ctx, api, state, sets, page_size, post_kinds)
            try:
                yield from run.seeds()  # cheap (one user object per seed) and needed by every phase
                if "following" in phases:
                    yield from run.following()
                # Coverage of the seeds' direct following comes first: posts and expansion only
                # spend budget once every seed is complete -- or when the operator explicitly runs
                # them without the following phase (X_SPIDER_PHASES=posts).
                if state.seeds_complete(sets) or "following" not in phases:
                    if "posts" in phases:
                        yield from run.posts()
                    if "expansion" in phases:
                        yield from run.expansion()
                else:
                    state.note("waiting", "posts and expansion wait until every seed's following is complete")
            except XStop as e:
                ctx.log.warning("x_spider: X said stop: %s", e.reason)
                state.note("x_stop", e.reason)
            except BudgetExhausted as e:
                state.note("budget_stop", str(e))
            except _LimitReached:
                state.note("limit_stop", f"--limit {ctx.limit}")
        self.run_stats = state.run_stats()
        ctx.log.info("x_spider: run stats %s", self.run_stats)
        # A --limit stop may cut a page short: keep the progress of the last checkpoint only
        # (spend is always kept), so the next run re-reads that page instead of skipping it.
        yield CursorUpdate(cursor=state.cursor(saved_only="limit_stop" in state.notes))

    # ── extract (pure) ────────────────────────────────────────────────────
    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        p = rec.payload if isinstance(rec.payload, dict) else {}
        rtype = p.get("type")
        if rtype == "x_user":
            return user_claims(p.get("user") or {})
        if rtype == "x_following":
            return following_claims(p.get("seed") or {}, p.get("users") or [])
        return []  # x_post: for the parser


# ── fetch helpers ─────────────────────────────────────────────────────────


class _LimitReached(Exception):
    pass


class _State:
    """The cursor, as the spider reads and updates it."""

    def __init__(self, cur: dict[str, Any], budget: XBudget) -> None:
        self.budget = budget
        self.seeds: dict[str, str] = dict(cur.get("seeds") or {})  # username -> id
        self.seed_users: dict[str, dict[str, Any]] = {}
        # x user id -> {"next": token|None, "done": bool, "pages": n, "users": n, "depth": d, ...}
        self.crawl: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (cur.get("crawl") or {}).items()}
        self.posts_done: list[str] = list(cur.get("posts_done") or [])
        self.notes: dict[str, Any] = {}
        self.records = 0
        self.saved = copy.deepcopy(self._progress())  # progress as of the last CursorUpdate

    def _progress(self) -> dict[str, Any]:
        return {"seeds": self.seeds, "crawl": self.crawl, "posts_done": self.posts_done}

    def checkpoint(self) -> CursorUpdate:
        """Everything yielded so far is covered: remember it and hand the pipeline the cursor."""
        self.saved = copy.deepcopy(self._progress())
        return CursorUpdate(cursor=self.cursor())

    def note(self, key: str, value: Any) -> None:
        self.notes[key] = value

    def seeds_complete(self, sets: Mapping[str, list[str]]) -> bool:
        for handles in sets.values():
            for h in handles:
                uid = self.seeds.get(h)
                if uid is None:
                    return False
                if uid == "missing":
                    continue
                c = self.crawl.get(uid)
                if not c or not c.get("done"):
                    return False
        return True

    def run_stats(self) -> dict[str, Any]:
        r, t = self.budget.run, self.budget.total
        return {
            "api_calls": r.api_calls,
            "cost_usd": round(r.usd, 4),
            "user_reads": r.user_reads,
            "post_reads": r.post_reads,
            "x_total_spent_usd": round(t.usd, 4),
            "x_total_budget_usd": self.budget.total_usd,
            **self.notes,
        }

    def cursor(self, *, saved_only: bool = False) -> dict[str, Any]:
        progress = self.saved if saved_only else self._progress()
        return {
            **copy.deepcopy(progress),
            "spend": self.budget.total.as_dict(),
            "last_run": self.run_stats(),
        }


class _Run:
    def __init__(
        self,
        spider: XSpider,
        ctx: Ctx,
        api: XApi,
        state: _State,
        sets: Mapping[str, list[str]],
        page_size: int,
        post_kinds: set[str],
    ) -> None:
        self.spider, self.ctx, self.api, self.state = spider, ctx, api, state
        self.sets, self.page_size, self.post_kinds = sets, page_size, post_kinds

    def _record(self, rec: RawRecord) -> RawRecord:
        if self.ctx.limit is not None and self.state.records >= self.ctx.limit:
            raise _LimitReached
        self.state.records += 1
        return rec

    # 1. seeds
    def seeds(self) -> Iterator[RawRecord | CursorUpdate]:
        todo = sorted({h for hs in self.sets.values() for h in hs} - set(self.state.seeds))
        for i in range(0, len(todo), 100):
            batch = todo[i : i + 100]
            users = self.api.users_by_usernames(batch)
            found = {str(u.get("username", "")).lower(): u for u in users}
            for h in batch:
                u = found.get(h)
                if u is None:
                    self.ctx.log.warning("x_spider: seed @%s not found", h)
                    self.state.seeds[h] = "missing"
                    continue
                self.state.seeds[h] = str(u["id"])
                self.state.seed_users[str(u["id"])] = u
                yield self._record(user_record(u))
            self._frontier_seeds()
            yield self.state.checkpoint()

    def _frontier_seeds(self) -> None:
        items = []
        for set_name, handles in self.sets.items():
            for h in handles:
                uid = self.state.seeds.get(h)
                if not uid or uid == "missing":
                    continue
                u = self.state.seed_users.get(uid)
                items.append(
                    frontier.Item(
                        key=uid,
                        kind=classify(u) if u else "person",
                        label=h,
                        discovered_by=[f"seed-set:{set_name}"],
                        depth=0,
                        payload=u,
                    )
                )
        self._upsert(items)

    def _seed_user(self, uid: str) -> dict[str, Any] | None:
        if uid in self.state.seed_users:
            return self.state.seed_users[uid]
        if self.ctx.db is None:
            return None
        with self.ctx.db.view() as conn:
            row = conn.execute(
                "select payload from frontier where source = %s and key = %s", (SOURCE, uid)
            ).fetchone()
        u = row["payload"] if row and isinstance(row["payload"], dict) else None
        if u:
            self.state.seed_users[uid] = u
        return u

    # 2. direct following, round robin over seeds
    def following(self) -> Iterator[RawRecord | CursorUpdate]:
        order: list[tuple[str, str, str]] = []  # (set, username, id); a seed in two sets is crawled once
        for set_name, handles in self.sets.items():
            for h in handles:
                uid = self.state.seeds.get(h)
                if uid and uid != "missing" and uid not in {o[2] for o in order}:
                    order.append((set_name, h, uid))
        while True:
            active = [o for o in order if not self.state.crawl.get(o[2], {}).get("done")]
            if not active:
                return
            # Share what is left evenly, so a small budget covers every seed's first page instead
            # of spending it all on the first seed (pages resume via pagination_token next run).
            left = self.api.budget.users_allowed(10**9)
            share = max(1, -(-left // len(active)))
            for set_name, handle, uid in active:
                yield from self._page(uid, handle, depth=0, seed_set=set_name, want=share)

    def _page(
        self, uid: str, handle: str, *, depth: int, seed_set: str | None, want: int | None = None
    ) -> Iterator[RawRecord | CursorUpdate]:
        """One page of ``uid``'s following; raises BudgetExhausted when not even one user fits."""
        c = self.state.crawl.setdefault(
            uid, {"next": None, "done": False, "pages": 0, "users": 0, "depth": depth}
        )
        if c.get("done"):
            return
        n = self.api.budget.users_allowed(min(self.page_size, want or self.page_size))
        if n < 1:
            raise BudgetExhausted(
                f"no user reads left (run {self.api.budget.run.user_reads}/{self.api.budget.run_user_cap}, "
                f"total ${self.api.budget.total.usd:.2f}/${self.api.budget.total_usd:.2f})"
            )
        try:
            users, nxt = self.api.following(uid, max_results=n, token=c.get("next"))
        except XAccountError as e:
            self.ctx.log.warning("x_spider: following of @%s failed: %s; skipping", handle, e)
            c.update(done=True, error=str(e)[:200])
            self._mark([uid], "failed")
            yield self.state.checkpoint()
            return
        seed_user = self._seed_user(uid) or {"id": uid, "username": handle}
        page_no = c["pages"]
        c["pages"] += 1
        c["users"] += len(users)
        c["next"] = nxt
        c["done"] = not nxt
        if c["done"]:
            c["completed_at"] = _now()
        items = [
            frontier.Item(
                key=str(u["id"]),
                kind=classify(u),
                label=str(u.get("username") or "") or None,
                discovered_by=[handle],
                depth=depth + 1,
                payload=u,
            )
            for u in users
            if u.get("id")
        ]
        self._upsert(items)
        if c["done"]:
            self._mark([uid], "fetched")
        # Profiles first, so each account is minted by its own x_user record (with its name).
        for u in users:
            yield self._record(user_record(u))
        yield self._record(
            RawRecord.new(
                SOURCE,
                f"following:{uid}:{page_no}",
                {
                    "type": "x_following",
                    "seed": seed_user,
                    "seed_set": seed_set,
                    "depth": depth,
                    "page": page_no,
                    "users": users,
                },
                url=f"https://x.com/{handle}/following",
            )
        )
        yield self.state.checkpoint()

    # 3. posts
    def posts(self) -> Iterator[RawRecord | CursorUpdate]:
        if self.api.budget.run_post_cap <= 0 or not self.post_kinds:
            return
        while True:
            want = self.api.budget.posts_allowed(POSTS_PER_ACCOUNT)
            if want < 5:  # the API's smallest page is 5 posts
                self.state.note("posts_stop", "post budget spent")
                return
            nxt = self._next_post_account()
            if nxt is None:
                return
            uid, handle = nxt
            try:
                posts = self.api.tweets(uid, max_results=want)
            except XAccountError as e:
                self.ctx.log.warning("x_spider: posts of @%s failed: %s", handle, e)
                posts = []
            self.state.posts_done.append(uid)
            author = {"id": uid, "username": handle}
            for t in posts[:want]:
                yield self._record(
                    RawRecord.new(
                        SOURCE,
                        f"post:{t['id']}",
                        {"type": "x_post", "post": t, "author": author},
                        url=f"https://x.com/{handle}/status/{t['id']}",
                    )
                )
            yield self.state.checkpoint()

    def _next_post_account(self) -> tuple[str, str] | None:
        if self.ctx.db is None:
            return None
        with self.ctx.db.view() as conn:
            for kind in sorted(self.post_kinds):
                rows = frontier.top(
                    conn, SOURCE, limit=1, status=None, kind=kind, min_depth=1, exclude=self.state.posts_done
                )
                if rows:
                    return str(rows[0]["key"]), str(rows[0]["label"] or rows[0]["key"])
        return None

    # 4. depth-1 expansion
    def expansion(self) -> Iterator[RawRecord | CursorUpdate]:
        while True:
            current = self._current_expansion()
            if current is None:
                self.state.note("expansion", "no eligible accounts left")
                return
            uid, handle = current
            self.state.crawl.setdefault(
                uid, {"next": None, "done": False, "pages": 0, "users": 0, "depth": 1, "expansion": True}
            )
            while not self.state.crawl[uid].get("done"):
                yield from self._page(uid, handle, depth=1, seed_set=None)

    def _current_expansion(self) -> tuple[str, str] | None:
        for uid, c in self.state.crawl.items():  # an unfinished one from an earlier run first
            if c.get("expansion") and not c.get("done"):
                u = self._seed_user(uid) or {}
                return uid, str(u.get("username") or uid)
        if self.ctx.db is None:
            return None
        with self.ctx.db.view() as conn:
            rows = frontier.top(conn, SOURCE, limit=EXPANSION_CANDIDATES, kind="person", max_depth=1)
        for r in rows:
            if r["depth"] < 1 or str(r["key"]) in self.state.crawl:
                continue
            if expansion_eligible(r["payload"] or {}, int(r["score"])):
                self.state.seed_users[str(r["key"])] = r["payload"] or {}
                return str(r["key"]), str(r["label"] or r["key"])
        return None

    # frontier
    def _upsert(self, items: list[frontier.Item]) -> None:
        if self.ctx.db is None or not items:
            return
        with self.ctx.db.view() as conn:
            frontier.upsert(conn, SOURCE, items)

    def _mark(self, keys: list[str], status: str) -> None:
        if self.ctx.db is None:
            return
        with self.ctx.db.view() as conn:
            frontier.mark(conn, SOURCE, keys, status)


def _persist_spend(ctx: Ctx, total: Spend) -> None:
    """Write the cumulative spend into connector_state right away (survives a crashed run)."""
    if ctx.db is None:
        return
    with ctx.db.view() as conn:
        conn.execute(
            """insert into connector_state (source, interval_s, cursor) values (%s, %s, %s)
               on conflict (source) do update
                  set cursor = jsonb_set(coalesce(connector_state.cursor, '{}'::jsonb), '{spend}', %s)""",
            (SOURCE, XSpider.interval_s, Jsonb({"spend": total.as_dict()}), Jsonb(total.as_dict())),
        )


def user_record(u: dict[str, Any]) -> RawRecord:
    handle = str(u.get("username") or "")
    return RawRecord.new(
        SOURCE, f"user:{u['id']}", {"type": "x_user", "user": u}, url=f"https://x.com/{handle}"
    )


def expansion_eligible(u: dict[str, Any], score: int) -> bool:
    """SF people: location or bio mentions SF / the Bay Area, or no location and >= 2 seeds."""
    if classify(u) != "person":
        return False
    loc = _clean(u.get("location")) or ""
    if SF_RE.search(loc) or SF_RE.search(_clean(u.get("description")) or ""):
        return True
    return not loc and score >= 2


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _num(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


# ── extract (pure) ────────────────────────────────────────────────────────


def classify(u: Mapping[str, Any]) -> str:
    """``company`` or ``person`` from the user object alone (deterministic, conservative).

    Company when ``verified_type`` is business/government, the bio says "official account", or
    company signals (company-like name, the website is the account's own name/handle, "we"/"our"
    voice, product phrases) outweigh person signals (first person singular, pronouns, roles).
    """
    vt = str(u.get("verified_type") or "").lower()
    if vt in ("business", "government"):
        return "company"
    name = _clean(u.get("name")) or ""
    bio = _clean(u.get("description")) or ""
    if _OFFICIAL_RE.search(bio):
        return "company"
    company = person = 0
    if _COMPANY_NAME_RE.search(_strip_symbols(name)):
        company += 2
    site = own_site_domain(u)
    if site is not None:
        company += 1
    if _WE_RE.search(bio):
        company += 1
    if _COMPANY_BIO_RE.search(bio):
        company += 1
    if _PERSON_BIO_RE.search(bio):
        person += 2
    if _looks_like_person_name(name):
        person += 1
    return "company" if company >= 2 and company > person else "person"


def own_site_domain(u: Mapping[str, Any]) -> str | None:
    """The domain of the profile website when it is the account's own: its host label equals
    the handle or the name (allowing get-/try- prefixes and -ai/-labs/-hq... suffixes)."""
    site = expanded_url(u)
    if not site:
        return None
    dom = site_domain(site, allow_platform_root=True)
    if dom is None or dom in LINK_HOSTS:
        return None
    label = _key(_host_label(dom))
    if len(label) < 2:
        return None
    for base in {_key(str(u.get("username") or "")), _key(_strip_symbols(_clean(u.get("name")) or ""))}:
        if len(base) < 2:
            continue
        if (
            label == base
            or label in {p + base for p in _HOST_PREFIXES}
            or label in {base + s for s in _NAME_SUFFIXES}
        ):
            return dom
        for s in _NAME_SUFFIXES:  # handle "acmehq" / name "Acme AI" -> acme.com
            if base.endswith(s) and len(base) - len(s) >= 3 and label == base[: -len(s)]:
                return dom
    name = _strip_symbols(_clean(u.get("name")) or "")
    if name and name_matches_host(name, dom):  # "Nao Labs" ~ getnao.io
        return dom
    return None


def user_claims(u: Mapping[str, Any]) -> list[ClaimInput]:
    uid = str(u.get("id") or "")
    handle = str(u.get("username") or "").lower()
    if not uid or not handle:
        return []
    kind = classify(u)
    ref = user_ref(u)
    out: list[ClaimInput] = []

    def add(predicate: str, value: Any) -> None:
        if value is None or value == "":
            return
        out.append(
            ClaimInput(
                subject=ref, predicate=predicate, value=value, source_ref=f"{SOURCE}:user:{uid}:{predicate}"
            )
        )

    add("x_id", uid)
    add("x_handle", handle)
    add("name", display_name(u))
    bio = bio_text(u)
    add("headline" if kind == "person" else "description", bio)
    if kind == "person":
        add("location", _clean(u.get("location")))
        add("website", expanded_url(u))
    else:
        dom = own_site_domain(u)
        if dom:
            add("website", expanded_url(u))
            add("domain", dom)
    add("avatar", avatar_url(u.get("profile_image_url")))
    followers = (u.get("public_metrics") or {}).get("followers_count")
    if isinstance(followers, int) and followers >= 0:
        add("x_followers", followers)
    return out


def user_ref(u: Mapping[str, Any]) -> EntityRef:
    kind = classify(u)
    identity: dict[str, Any] = {
        "x_id": str(u.get("id") or ""),
        "x_handle": str(u.get("username") or "").lower(),
    }
    if kind == "company":
        identity["domain"] = own_site_domain(u)
    return EntityRef(kind=kind, identity=identity, name=display_name(u) or str(u.get("username") or ""))


def following_claims(seed: Mapping[str, Any], users: list[Mapping[str, Any]]) -> list[ClaimInput]:
    sid = str(seed.get("id") or "")
    if not sid or not seed.get("username"):
        return []
    subject = user_ref(seed)
    return [
        ClaimInput(
            subject=subject,
            predicate="follows",
            object=user_ref(u),
            source_ref=f"{SOURCE}:follows:{sid}:{u['id']}",
        )
        for u in users
        if u.get("id") and u.get("username")
    ]


def display_name(u: Mapping[str, Any]) -> str | None:
    """The display name without emoji / pictographs ("Jane Doe 🌉" -> "Jane Doe")."""
    name = _clean(_strip_symbols(str(u.get("name") or "")))
    return name or (str(u.get("username")) if u.get("username") else None)


def bio_text(u: Mapping[str, Any]) -> str | None:
    """The bio with t.co links replaced by their expanded URLs."""
    bio = u.get("description")
    if not isinstance(bio, str):
        return None
    for e in ((u.get("entities") or {}).get("description") or {}).get("urls") or []:
        if isinstance(e, dict) and e.get("url") and e.get("expanded_url") and not _is_tco(e["expanded_url"]):
            bio = bio.replace(e["url"], e["expanded_url"])
    return _clean(bio)


def expanded_url(u: Mapping[str, Any]) -> str | None:
    """The profile website: ``entities.url.urls[].expanded_url`` matching ``url``; never t.co."""
    urls = ((u.get("entities") or {}).get("url") or {}).get("urls") or []
    short = u.get("url")
    for e in urls:
        if not isinstance(e, dict):
            continue
        exp = e.get("expanded_url")
        if exp and (not short or e.get("url") == short) and not _is_tco(exp):
            return exp
    if isinstance(short, str) and short and not _is_tco(short):
        return short
    return None


def avatar_url(v: object) -> str | None:
    """``..._normal.jpg`` (48px) -> ``..._400x400.jpg``; the default egg/silhouette is dropped."""
    if not isinstance(v, str) or not v.startswith("https://"):
        return None
    if "default_profile_images" in v:
        return None
    return re.sub(r"_(?:normal|bigger|mini)(\.[a-z]+)?$", r"_400x400\1", v)


def _looks_like_person_name(name: str) -> bool:
    parts = _strip_symbols(name).split()
    return (
        2 <= len(parts) <= 4
        and all(p[:1].isupper() and p.replace(".", "").replace("-", "").isalpha() for p in parts)
        and not _COMPANY_NAME_RE.search(_strip_symbols(name))
    )


def _strip_symbols(s: str) -> str:
    """Drop emoji, pictographs and other symbol characters (keeps letters, digits, punctuation)."""
    kept = "".join(
        ch
        for ch in s
        if not unicodedata.category(ch).startswith(("So", "Sk", "Cs", "Co", "Cn")) and ch not in "‍️"
    )
    return " ".join(kept.split())


def _host_label(host: str) -> str:
    labels = host.split(".")
    return labels[-2] if len(labels) >= 2 else host


def _is_tco(u: str) -> bool:
    return u.lower().split("://", 1)[-1].startswith("t.co/")


def _clean(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s or None
