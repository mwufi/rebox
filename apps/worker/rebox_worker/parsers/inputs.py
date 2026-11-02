"""Upstream raw records -> ``InputDoc`` (what the model reads and what quotes are checked against).

Accepted upstream shapes (tolerant: the spider slice may wrap objects):

- ``x_user``  an X API v2 user object (``description``, ``location``, ``name``, ``username``,
  ``id``, ``entities``), bare or under ``payload["user"]`` (the ``x`` connector's records).
- ``x_post``  an X API v2 tweet object (``text``, ``created_at``, ``author_id``, ``id``), bare or
  under ``payload["tweet"|"post"]`` (the spider: ``{"type": "x_post", "post", "author"}``); the
  author, when the record carries it, under
  ``payload["author"|"user"]`` or ``payload["includes"]["users"]``.
- ``github``  the github connector's ``{"user": profile, "orgs": {...}}``.
- ``exa_result`` ``url``, ``title``, ``published_date`` (or ``publishedDate``), ``text`` (or
  ``highlights``/``summary``), bare or under ``payload["result"]``; ``homepage: true`` results
  are company homepages, left to the deterministic ``exa`` connector.

Field values are HTML-unescaped (X returns ``&amp;``) and whitespace-trimmed; they are stored in
the parser's raw record so ``extract`` checks quotes against exactly what the model saw.
"""

from __future__ import annotations

import html
import re
from typing import Any

from ..models import RawRecord
from .prompt import InputDoc

ARTICLE_MAX_CHARS = 4000
POST_MIN_CHARS = 15

_HANDLE_X = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def clean(v: object, *, limit: int | None = None) -> str:
    if not isinstance(v, str):
        return ""
    s = html.unescape(v).replace("\r\n", "\n").strip()
    s = re.sub(r"[ \t]+", " ", s)
    if limit is not None and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0]
    return s


def x_user_of(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    u = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    return u if isinstance(u, dict) and (u.get("username") or u.get("id")) else None


def x_post_of(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for key in ("tweet", "post"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return payload if isinstance(payload.get("text"), str) and payload.get("id") else None


def post_author_of(payload: Any, post: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for key in ("author", "user"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    users = (
        (payload.get("includes") or {}).get("users") if isinstance(payload.get("includes"), dict) else None
    )
    for u in users or []:
        if isinstance(u, dict) and str(u.get("id")) == str(post.get("author_id")):
            return u
    return None


def classify(rec: RawRecord) -> str | None:
    """Which kind of input an upstream record is: x_user | x_post | github | exa_result | None.

    ``payload["type"]`` decides when present (the spider writes ``x_user``, ``x_post``,
    ``x_following``, ``exa_result``); otherwise the shape does.
    """
    p = rec.payload
    if not isinstance(p, dict):
        return None
    t = p.get("type")
    if t in ("x_user", "x_post", "exa_result"):
        return str(t)
    if t is not None:
        return None  # x_following and anything newer: not text to parse
    if rec.source == "github":
        return "github" if isinstance(p.get("user"), dict) else None
    if rec.source == "exa":
        r = p.get("result") if isinstance(p.get("result"), dict) else p
        return "exa_result" if isinstance(r, dict) and r.get("url") else None
    post = x_post_of(p)
    if post is not None and post.get("author_id") is not None:
        return "x_post"
    if x_user_of(p) is not None:
        return "x_user"
    return None


def date_of(v: object) -> str | None:
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return None


def _ref(rec: RawRecord) -> str:
    return f"{rec.source}:{rec.external_id}"


def doc_for(rec: RawRecord) -> InputDoc | None:
    """The document to parse for an upstream record, or None when there is nothing to parse."""
    kind = classify(rec)
    if kind == "x_user":
        return _x_user(rec)
    if kind == "x_post":
        return _x_post(rec)
    if kind == "github":
        return _github(rec)
    if kind == "exa_result":
        return _exa(rec)
    return None


def _x_user(rec: RawRecord) -> InputDoc | None:
    u = x_user_of(rec.payload) or {}
    handle = str(u.get("username") or "").lower()
    bio, location = clean(u.get("description")), clean(u.get("location"))
    if not bio and not location:
        return None
    graph = rec.payload.get("graph") if isinstance(rec.payload, dict) else None
    return InputDoc(
        ref=_ref(rec),
        kind="x_bio",
        fields={"name": clean(u.get("name")), "location": location, "bio": bio},
        meta={
            "handle": handle or None,
            "x_id": str(u["id"]) if u.get("id") else None,
            "url": f"https://x.com/{handle}" if handle else rec.url,
            "author": f"@{handle}" if handle else None,
            "observed": date_of(rec.fetched_at.isoformat()),
            "graph_kinds": sorted({g.get("kind") for g in graph or [] if isinstance(g, dict)} - {None}),
        },
    )


def _x_post(rec: RawRecord) -> InputDoc | None:
    t = x_post_of(rec.payload) or {}
    text = clean(t.get("text"))
    refs = {r.get("type") for r in t.get("referenced_tweets") or [] if isinstance(r, dict)}
    author = post_author_of(rec.payload, t) or {}
    if len(text) < POST_MIN_CHARS or text.startswith("RT @") or "retweeted" in refs:
        return None
    if "replied_to" in refs and not _self_thread(t, author):
        return None  # a reply to someone else; the author's own thread continuations are kept
    handle = str(author.get("username") or rec.payload.get("author_username") or "").lower() or None
    if handle and not _HANDLE_X.match(handle):
        handle = None
    tid = str(t.get("id"))
    return InputDoc(
        ref=_ref(rec),
        kind="x_post",
        fields={"text": text},
        meta={
            "handle": handle,
            "x_id": str(t.get("author_id") or author.get("id") or "") or None,
            "author_name": clean(author.get("name")) or None,
            "date": date_of(t.get("created_at")) or date_of(rec.fetched_at.isoformat()),
            "url": f"https://x.com/{handle}/status/{tid}" if handle else f"https://x.com/i/status/{tid}",
            "author": f"@{handle}" if handle else None,
        },
    )


def _self_thread(t: dict[str, Any], author: dict[str, Any]) -> bool:
    """A reply that continues the author's own thread (X: in_reply_to_user_id = author_id; the
    spider's ``exclude=replies`` already drops replies to others, so a missing field counts)."""
    to = t.get("in_reply_to_user_id")
    me = t.get("author_id") or author.get("id")
    return to is None or (me is not None and str(to) == str(me))


def _github(rec: RawRecord) -> InputDoc | None:
    u = rec.payload["user"]
    login = str(u.get("login") or "")
    if not login or u.get("type", "User") != "User":
        return None
    bio, company, location = clean(u.get("bio")), clean(u.get("company")), clean(u.get("location"))
    if not (bio or company or location):
        return None
    x = clean(u.get("twitter_username")).lstrip("@")
    return InputDoc(
        ref=_ref(rec),
        kind="github_bio",
        fields={"name": clean(u.get("name")), "company": company, "location": location, "bio": bio},
        meta={
            "handle": login,
            "github": login,
            "x_handle": x if _HANDLE_X.match(x or "-") else None,
            "url": f"https://github.com/{login}",
            "author": login,
            "observed": date_of(rec.fetched_at.isoformat()),
        },
    )


def _exa(rec: RawRecord) -> InputDoc | None:
    p = rec.payload
    r = p.get("result") if isinstance(p.get("result"), dict) else p
    text = clean(r.get("text"), limit=ARTICLE_MAX_CHARS)
    if not text:
        hl = r.get("highlights")
        text = clean(
            " … ".join(h for h in hl if isinstance(h, str)) if isinstance(hl, list) else r.get("summary")
        )
    title = clean(r.get("title"))
    if not (title or text) or r.get("homepage") is True:
        return None
    from ..connectors.hn import host_of

    url = str(r.get("url"))
    return InputDoc(
        ref=_ref(rec),
        kind="article",
        fields={"title": title, "text": text},
        meta={
            "date": date_of(r.get("published_date") or r.get("publishedDate")),
            "url": url,
            "author": clean(r.get("author")) or host_of(url),
            "host": host_of(url),
        },
    )
