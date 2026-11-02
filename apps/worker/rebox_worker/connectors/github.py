"""GitHub (``public/github``): people in San Francisco with ≥ 100 followers, and their ``@org``.

Live mode needs ``GITHUB_TOKEN``; without it ``enabled()`` is False (the scheduler skips the
connector) and ``fetch`` refuses to run. No GitHub access exists in this environment, so the
connector is built and tested against synthetic fixtures (``fixtures/raw/github/``, fake
logins such as ``example-octo-1``) served through an httpx mock transport.

fetch
    1. Search users ``location:"San Francisco" followers:<lo>..<hi>`` over ``FOLLOWER_RANGES``
       (the search API returns at most 1,000 results per query, so the space is split by
       follower count; a range that still reports > 1,000 results is logged as truncated).
    2. ``GET /users/<login>`` for each login (sorted, de-duplicated).
    3. For ``company`` values of the form ``@org``: ``GET /orgs/<org>`` (cached per run).
    One RawRecord per user (``external_id = user:<login lowercased>``), payload =
    ``{"user": <profile>, "orgs": {<org>: <org profile>}}``. Cursor = last login processed, so
    a run resumes after an interruption; a completed pass resets it.
    Rate limits: ``MIN_INTERVAL_S`` between requests; on 403/429 with rate-limit headers it
    sleeps until ``x-ratelimit-reset`` (capped), otherwise exponential backoff.

extract (pure)
    person   identity ``github`` (+ ``x_handle``); ``name`` (profile name, else the login),
             ``github``, ``x_handle``, ``website`` (blog), ``headline`` (bio), ``location``.
    company  only from ``@org``: identity ``github`` (+ ``domain`` of its blog, ``x_handle``);
             ``name``, ``github``, ``website``, ``domain``, ``x_handle``.
    edge     person ``worked_at`` company (no dates: GitHub has none). A plain-text company
             ("Acme Inc") is ignored: it is not an identity and would mint duplicates.

source_ref: ``github:user:<login>:<predicate>[:<key>]`` / ``github:org:<login>:<predicate>``.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from ..models import ClaimInput, CursorUpdate, EntityRef, RawRecord
from .base import Ctx
from .hn import get_json, site_domain

API = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"
LOCATION = "San Francisco"
FOLLOWER_RANGES = (
    (100, 149), (150, 199), (200, 299), (300, 499), (500, 999), (1000, 2999), (3000, None),
)  # fmt: skip
PER_PAGE = 100
SEARCH_CAP = 1000
MIN_INTERVAL_S = 1.0
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_ORG_RE = re.compile(r"^@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$")


def token(ctx: Ctx, name: str) -> str | None:
    """A token from the connector context, falling back to the process environment.

    (``jobs.schedule_due`` builds ``Ctx()`` without ``env``; see the S4 report.)"""
    return ctx.env.get(name) or os.environ.get(name) or None


class GitHub:
    name = "github"
    layer = "public/github"
    interval_s = 604800

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.transport = transport
        self.sleep = sleep
        self.min_interval_s = min_interval_s

    def enabled(self, ctx: Ctx) -> bool:
        return token(ctx, TOKEN_ENV) is not None

    # ── fetch: the only part that does I/O ──
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        tok = token(ctx, TOKEN_ENV)
        if tok is None:
            raise RuntimeError(f"github connector is disabled: set {TOKEN_ENV}")
        after = (cursor or {}).get("last_login") or ""
        headers = {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        with ctx.http(transport=self.transport, headers=headers) as client:

            def get(path: str, params: dict[str, str] | None = None) -> Any:
                return get_json(
                    client,
                    API + path,
                    params=params,
                    log=ctx.log,
                    sleep=self.sleep,
                    min_interval_s=self.min_interval_s,
                    retry_statuses=(403, 429),
                )

            logins = sorted({lg.lower(): lg for lg in self._search(get, ctx)}.items())
            todo = [lg for key, lg in logins if key > after.lower()]
            ctx.log.info("github: %d users found, %d after cursor %r", len(logins), len(todo), after)
            orgs: dict[str, Any] = {}
            n = 0
            for login in todo:
                if ctx.limit is not None and n >= ctx.limit:
                    return  # cursor stays at the last login, the next run continues from there
                user = get(f"/users/{login}")
                org_login = org_of(user.get("company"))
                if org_login and org_login.lower() not in orgs:
                    try:
                        orgs[org_login.lower()] = get(f"/orgs/{org_login}")
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code != 404:
                            raise
                        orgs[org_login.lower()] = None  # "@someone" that is a user, or gone
                org = orgs.get(org_login.lower()) if org_login else None
                payload = {
                    "user": _keep(user, USER_FIELDS),
                    "orgs": {org_login.lower(): _keep(org, ORG_FIELDS)} if org_login and org else {},
                }
                n += 1
                yield RawRecord.new(
                    self.name, f"user:{login.lower()}", payload, url=f"https://github.com/{login}"
                )
                yield CursorUpdate(cursor={"last_login": login.lower()})
        yield CursorUpdate(cursor={"last_login": None, "last_run_users": n})  # pass complete

    def _search(self, get: Callable[..., Any], ctx: Ctx) -> Iterator[str]:
        for lo, hi in FOLLOWER_RANGES:
            rng = f"{lo}..{hi}" if hi is not None else f">={lo}"
            q = f'location:"{LOCATION}" followers:{rng} type:user'
            page = 1
            while True:
                data = get("/search/users", {"q": q, "per_page": str(PER_PAGE), "page": str(page)})
                items = data.get("items") or []
                for it in items:
                    if it.get("login") and _LOGIN_RE.match(it["login"]):
                        yield it["login"]
                total = int(data.get("total_count") or 0)
                if total > SEARCH_CAP and page == 1:
                    ctx.log.warning("github: %s has %d results; only %d reachable", rng, total, SEARCH_CAP)
                if len(items) < PER_PAGE or page * PER_PAGE >= min(total, SEARCH_CAP):
                    break
                page += 1

    # ── extract: pure ──
    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        u: dict[str, Any] = rec.payload.get("user") or {}
        login = u.get("login")
        if not login or not _LOGIN_RE.match(login) or u.get("type", "User") != "User":
            return []
        lk = login.lower()
        out: list[ClaimInput] = []

        def add(subject: EntityRef, predicate: str, ref: str, **kw: Any) -> None:
            out.append(
                ClaimInput(subject=subject, predicate=predicate, source_ref=f"{self.name}:{ref}", **kw)
            )

        org_payload = None
        org_login = org_of(u.get("company"))
        if org_login:
            org_payload = (rec.payload.get("orgs") or {}).get(org_login.lower())
        company = _org_ref(org_payload) if org_payload else None

        x = _x(u.get("twitter_username"))
        name = _clean(u.get("name")) or login
        person = EntityRef(
            kind="person",
            identity={"github": login, "x_handle": x},
            name=name,
            context=[company] if company is not None else None,
        )
        add(person, "name", f"user:{lk}:name", value=name)
        add(person, "github", f"user:{lk}:github", value=login)
        if x:
            add(person, "x_handle", f"user:{lk}:x_handle", value=x)
        if site := _website(u.get("blog")):
            add(person, "website", f"user:{lk}:website", value=site)
        if bio := _clean(u.get("bio")):
            add(person, "headline", f"user:{lk}:headline", value=bio)
        if loc := _clean(u.get("location")):
            add(person, "location", f"user:{lk}:location", value=loc)

        if company is not None and org_payload is not None:
            ok = org_payload["login"].lower()
            oname = _clean(org_payload.get("name")) or org_payload["login"]
            add(company, "name", f"org:{ok}:name", value=oname)
            add(company, "github", f"org:{ok}:github", value=org_payload["login"])
            if site := _website(org_payload.get("blog")):
                add(company, "website", f"org:{ok}:website", value=site)
                if d := site_domain(site):
                    add(company, "domain", f"org:{ok}:domain", value=d)
            if ox := _x(org_payload.get("twitter_username")):
                add(company, "x_handle", f"org:{ok}:x_handle", value=ox)
            add(person, "worked_at", f"user:{lk}:worked_at:{ok}", object=company)
        return out


USER_FIELDS = ("login", "id", "type", "name", "company", "blog", "location", "bio", "twitter_username",
               "followers", "public_repos", "created_at", "updated_at")  # fmt: skip
ORG_FIELDS = ("login", "id", "name", "blog", "location", "description", "twitter_username", "created_at")


def _keep(obj: Any, fields: tuple[str, ...]) -> Any:
    if not isinstance(obj, dict):
        return obj
    return {k: obj[k] for k in fields if obj.get(k) not in (None, "")}


def org_of(company: object) -> str | None:
    """``"@acme-corp"`` -> ``acme-corp``; plain text, several orgs or junk -> None."""
    if not isinstance(company, str):
        return None
    m = _ORG_RE.match(company.strip())
    return m.group(1) if m else None


def _org_ref(org: dict[str, Any]) -> EntityRef:
    site = _website(org.get("blog"))
    identity = {"github": org["login"], "domain": site_domain(site) if site else None,
                "x_handle": _x(org.get("twitter_username"))}  # fmt: skip
    return EntityRef(kind="company", identity=identity, name=_clean(org.get("name")) or org["login"])


def _clean(v: object) -> str | None:
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s or None


def _x(v: object) -> str | None:
    s = _clean(v)
    if s is None:
        return None
    s = s.lstrip("@")
    return s if re.fullmatch(r"[A-Za-z0-9_]{1,15}", s) else None


def _website(v: object) -> str | None:
    """GitHub's ``blog`` is free text: keep it only if it looks like a URL."""
    s = _clean(v)
    if s is None or " " in s or "." not in s or "@" in s:
        return None
    return s if re.match(r"^https?://", s, re.I) else f"https://{s}"
