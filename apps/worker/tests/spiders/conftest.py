"""Spider tests run offline (network blocked, as in tests/connectors).

``FakeX`` is an in-memory X API v2 (users/by, following with pagination, tweets) behind an
``httpx.MockTransport``; it counts the user objects and posts it hands out, so budget tests can
check exactly what a run would have cost. ``fake_exa`` does the same for Exa search.
DB tests reuse the worker's conftest (set ``S2_OWNER_URL`` / ``S2_APP_URL`` to an ``s8_`` DB).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from connectors.conftest import FIXTURES, ctx, no_network  # noqa: F401  (autouse fixture)


def user(
    uid: str,
    username: str,
    *,
    name: str | None = None,
    bio: str = "",
    location: str | None = None,
    url: str | None = None,
    verified_type: str | None = None,
    followers: int = 100,
) -> dict[str, Any]:
    u: dict[str, Any] = {
        "id": uid,
        "username": username,
        "name": name or username.title(),
        "description": bio,
        "profile_image_url": f"https://pbs.twimg.com/profile_images/{uid}/a_normal.jpg",
        "public_metrics": {"followers_count": followers, "following_count": 10},
    }
    if location is not None:
        u["location"] = location
    if url:
        u["url"] = f"https://t.co/{uid}"
        u["entities"] = {"url": {"urls": [{"url": f"https://t.co/{uid}", "expanded_url": url}]}}
    if verified_type:
        u["verified_type"] = verified_type
    return u


class FakeX:
    """``following[id]`` = list of user objects; pages are cut by the request's max_results."""

    def __init__(self, users: list[dict[str, Any]], following: dict[str, list[dict[str, Any]]]) -> None:
        self.users = {u["username"].lower(): u for u in users}
        self.following = following
        self.tweets: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []
        self.user_objects = 0
        self.post_objects = 0
        self.fail: dict[str, httpx.Response] = {}  # path prefix -> canned response (once)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token"
        path = request.url.path
        self.calls.append(f"{path}?{request.url.query.decode()}")
        for prefix, resp in list(self.fail.items()):
            if path.startswith(prefix):
                del self.fail[prefix]
                return resp
        params = request.url.params
        if path == "/2/users/by":
            found = [self.users[h] for h in params["usernames"].split(",") if h in self.users]
            self.user_objects += len(found)
            return httpx.Response(200, json={"data": found} if found else {"errors": [{"detail": "nf"}]})
        parts = path.split("/")
        if len(parts) == 5 and parts[4] == "following":
            allu = self.following.get(parts[3], [])
            start = int(params.get("pagination_token", "0"))
            n = int(params["max_results"])
            assert 1 <= n <= 1000
            page = allu[start : start + n]
            self.user_objects += len(page)
            meta: dict[str, Any] = {"result_count": len(page)}
            if start + n < len(allu):
                meta["next_token"] = str(start + n)
            return httpx.Response(200, json={"data": page, "meta": meta} if page else {"meta": meta})
        if len(parts) == 5 and parts[4] == "tweets":
            assert params["exclude"] == "replies,retweets"
            n = int(params["max_results"])
            assert 5 <= n <= 100
            ts = self.tweets.get(parts[3], [])[:n]
            self.post_objects += len(ts)
            return httpx.Response(200, json={"data": ts, "meta": {"result_count": len(ts)}})
        return httpx.Response(404, json={"title": "Not Found"})

    @property
    def cost(self) -> float:
        return round(self.user_objects * 0.01 + self.post_objects * 0.005, 4)


def tweet(tid: str, text: str) -> dict[str, Any]:
    return {"id": tid, "text": text, "created_at": "2026-10-30T12:00:00.000Z"}


def fake_exa(results: Callable[[dict[str, Any]], list[dict[str, Any]]], bodies: list[dict[str, Any]]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.headers["x-api-key"] == "fake-exa"
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"results": results(body), "costDollars": {"total": 0.012}})

    return httpx.MockTransport(handler)


@pytest.fixture
def no_sleep() -> Callable[[float], None]:
    return lambda s: None


@pytest.fixture
def ctx_factory():
    return ctx


@pytest.fixture
def db(app_url, clean, owner_url):
    """The worker conftest's ``db``, plus a clean frontier (its ``clean`` predates migration 0005)."""
    import psycopg

    from rebox_worker.db import Db

    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute("truncate frontier")
    d = Db(app_url, max_size=4)
    yield d
    d.close()
