"""Shared X API v2 client with a hard spend guard (used by the ``x_spider`` connector).

X is pay-per-use and the account's credit is small, so every call goes through ``XBudget``:

* **Per run:** ``X_MAX_USER_READS_PER_RUN`` user objects and ``X_MAX_POST_READS_PER_RUN`` posts.
* **In total:** ``X_TOTAL_BUDGET_USD`` (default 30) of *estimated* spend, accumulated across runs
  in the connector's state (``Spend``, persisted after every call by the caller). A request is
  only sent when its worst case (``max_results`` objects) still fits; page sizes shrink to fit.
* **Prices:** $0.01 per user object, $0.005 per post (``USER_USD`` / ``POST_USD``), charged on
  the objects actually returned. X sends no cost headers today; if it ever does, they win.
* **Stop, don't retry,** on 402 (payment required), 429 (rate limited), 403 with a usage-cap /
  credit message, or ``x-rate-limit-remaining: 0`` after a success: ``XStop`` carries the reason,
  the spider ends the run cleanly and records it in the run stats. 5xx / network errors are
  retried a few times with backoff.

Never logs the token or request headers.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

API = "https://api.x.com/2"
TOKEN_ENV = "X_BEARER_TOKEN"
USER_FIELDS = "description,location,url,entities,public_metrics,profile_image_url,verified_type,created_at"
TWEET_FIELDS = "created_at,entities,public_metrics,lang,conversation_id,referenced_tweets"

USER_USD = 0.01
POST_USD = 0.005
DEFAULT_TOTAL_BUDGET_USD = 30.0
DEFAULT_RUN_USER_READS = 2000
DEFAULT_RUN_POST_READS = 1000

MIN_INTERVAL_S = 1.0
MAX_RETRIES = 3
CREDIT_MARKERS = ("usagecap", "usage cap", "credit", "payment", "billing", "spend", "client-not-enrolled")

log = logging.getLogger(__name__)


class XStop(RuntimeError):
    """X said stop (402 / 429 / credit / exhausted rate window). Not retried; ends the run."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class BudgetExhausted(RuntimeError):
    """The next request would exceed the per-run caps or the total spend budget."""


@dataclass
class Spend:
    """Cumulative estimated X spend (persisted in ``connector_state.cursor.spend``)."""

    user_reads: int = 0
    post_reads: int = 0
    api_calls: int = 0
    usd: float = 0.0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> Spend:
        d = d or {}
        return cls(
            user_reads=int(d.get("user_reads") or 0),
            post_reads=int(d.get("post_reads") or 0),
            api_calls=int(d.get("api_calls") or 0),
            usd=float(d.get("usd") or 0.0),
        )

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["usd"] = round(self.usd, 4)
        return out


@dataclass
class XBudget:
    """Per-run caps plus the persistent total. ``total`` is shared across runs; ``run`` is this run."""

    total_usd: float = DEFAULT_TOTAL_BUDGET_USD
    run_user_cap: int = DEFAULT_RUN_USER_READS
    run_post_cap: int = DEFAULT_RUN_POST_READS
    total: Spend = field(default_factory=Spend)
    run: Spend = field(default_factory=Spend)
    on_charge: Callable[[Spend], None] | None = None  # persist ``total`` after every call

    @classmethod
    def from_env(cls, env: Mapping[str, str], total: Spend | None = None) -> XBudget:
        return cls(
            total_usd=_num(env.get("X_TOTAL_BUDGET_USD"), DEFAULT_TOTAL_BUDGET_USD),
            run_user_cap=int(_num(env.get("X_MAX_USER_READS_PER_RUN"), DEFAULT_RUN_USER_READS)),
            run_post_cap=int(_num(env.get("X_MAX_POST_READS_PER_RUN"), DEFAULT_RUN_POST_READS)),
            total=total or Spend(),
        )

    def users_allowed(self, want: int) -> int:
        """How many user objects the next request may ask for (0 = stop)."""
        by_run = self.run_user_cap - self.run.user_reads
        by_total = int((self.total_usd - self.total.usd) / USER_USD + 1e-9)
        return max(0, min(want, by_run, by_total))

    def posts_allowed(self, want: int) -> int:
        by_run = self.run_post_cap - self.run.post_reads
        by_total = int((self.total_usd - self.total.usd) / POST_USD + 1e-9)
        return max(0, min(want, by_run, by_total))

    def charge(self, *, users: int = 0, posts: int = 0, usd: float | None = None) -> None:
        cost = usd if usd is not None else users * USER_USD + posts * POST_USD
        for s in (self.run, self.total):
            s.api_calls += 1
            s.user_reads += users
            s.post_reads += posts
            s.usd += cost
        if self.on_charge is not None:
            self.on_charge(self.total)


class XApi:
    """Thin GET client. Callers check the budget, call, then ``budget.charge`` what came back."""

    def __init__(
        self,
        client: httpx.Client,
        budget: XBudget,
        *,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_INTERVAL_S,
        log: logging.Logger = log,
    ) -> None:
        self.client = client
        self.budget = budget
        self.sleep = sleep
        self.min_interval_s = min_interval_s
        self.log = log

    # ── endpoints ──
    def users_by_usernames(self, usernames: list[str]) -> list[dict[str, Any]]:
        n = self.budget.users_allowed(len(usernames))
        if n < len(usernames):
            raise BudgetExhausted(f"user lookup of {len(usernames)} needs budget for {len(usernames)}")
        data = self._get("/users/by", {"usernames": ",".join(usernames), "user.fields": USER_FIELDS})
        users = [u for u in data.get("data") or [] if isinstance(u, dict)]
        self.budget.charge(users=len(users))
        return users

    def following(
        self, user_id: str, *, max_results: int, token: str | None
    ) -> tuple[list[dict], str | None]:
        """One page of accounts ``user_id`` follows; ``max_results`` must already fit the budget."""
        params = {"max_results": str(max_results), "user.fields": USER_FIELDS}
        if token:
            params["pagination_token"] = token
        data = self._get(f"/users/{user_id}/following", params)
        users = [u for u in data.get("data") or [] if isinstance(u, dict)]
        self.budget.charge(users=len(users))
        if not users and data.get("errors") and "data" not in data:
            raise XAccountError(_err_text(data))
        return users, (data.get("meta") or {}).get("next_token")

    def tweets(self, user_id: str, *, max_results: int) -> list[dict[str, Any]]:
        """Recent original posts (no replies, no retweets); ``max_results`` in 5..100."""
        params = {
            "max_results": str(max(5, min(100, max_results))),
            "exclude": "replies,retweets",
            "tweet.fields": TWEET_FIELDS,
        }
        data = self._get(f"/users/{user_id}/tweets", params)
        posts = [t for t in data.get("data") or [] if isinstance(t, dict)]
        self.budget.charge(posts=len(posts))
        if not posts and data.get("errors") and "data" not in data:
            raise XAccountError(_err_text(data))
        return posts

    # ── transport ──
    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if self.window_exhausted:
            raise XStop(f"rate-limit window exhausted (x-rate-limit-reset {self._window_reset})", 429)
        error = ""
        for attempt in range(MAX_RETRIES + 1):
            self.sleep(self.min_interval_s)
            try:
                resp = self.client.get(API + path, params=params)
            except httpx.TransportError as e:
                error = repr(e)
            else:
                if resp.status_code == 200:
                    self._check_window(resp)
                    return resp.json()
                stop = stop_reason(resp)
                if stop is not None:
                    raise XStop(stop, resp.status_code)
                if resp.status_code < 500:
                    raise XAccountError(f"HTTP {resp.status_code}: {_body_text(resp)}")
                error = f"HTTP {resp.status_code}"
            if attempt == MAX_RETRIES:
                break
            delay = min(30.0, 2.0 * 2**attempt)
            self.log.warning(
                "x: %s on %s; retry %d/%d in %.0fs", error, path, attempt + 1, MAX_RETRIES, delay
            )
            self.sleep(delay)
        raise XStop(f"x: giving up after {MAX_RETRIES + 1} attempts: {error}")

    def _check_window(self, resp: httpx.Response) -> None:
        self._window_reset = (
            resp.headers.get("x-rate-limit-reset", "?")
            if resp.headers.get("x-rate-limit-remaining") == "0"
            else None
        )

    @property
    def window_exhausted(self) -> bool:
        """True when the last successful response said the rate window is used up."""
        return getattr(self, "_window_reset", None) is not None


class XAccountError(RuntimeError):
    """A per-account problem (protected, suspended, not found): skip that account, keep going."""


def stop_reason(resp: httpx.Response) -> str | None:
    """Why X wants us to stop, or None for an ordinary error."""
    if resp.status_code == 401:
        return f"HTTP 401 unauthorized: {_body_text(resp)}"
    if resp.status_code == 402:
        return f"HTTP 402 payment required: {_body_text(resp)}"
    if resp.status_code == 429:
        return f"HTTP 429 rate limited (reset {resp.headers.get('x-rate-limit-reset', '?')})"
    if resp.status_code == 403:
        text = _body_text(resp)
        if any(m in text.lower().replace("_", "") for m in CREDIT_MARKERS):
            return f"HTTP 403 credit/usage cap: {text}"
    return None


def _body_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    return _err_text(data) if isinstance(data, dict) else str(data)[:200]


def _err_text(data: dict[str, Any]) -> str:
    parts = [str(data.get(k)) for k in ("title", "detail", "reason") if data.get(k)]
    for e in data.get("errors") or []:
        if isinstance(e, dict):
            parts.append(str(e.get("detail") or e.get("title") or e.get("message") or ""))
    return "; ".join(p for p in parts if p)[:300] or "error"


def token(env: Mapping[str, str]) -> str | None:
    v = (env.get(TOKEN_ENV) or "").strip()
    return v or None


def _num(v: str | None, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default
