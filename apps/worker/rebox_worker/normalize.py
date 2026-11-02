"""Identity value normalizers (CONTRACTS §4) and name normalization for fuzzy matching.

Identity values are stored normalized, so the resolver can match them with a plain
equality lookup. Every normalizer is idempotent: normalize(normalize(x)) == normalize(x).
Normalizers raise ``ValueError`` for values that cannot be normalized.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

# Both services implement these rules character for character (apps/api/src/normalize.ts); the
# shared cases in contracts/normalize-cases.json pin them down. Hence no urlsplit (its quirks have
# no TypeScript twin), ASCII-only case-insensitive regexes (re.A), explicit whitespace, fullmatch.

# Whitespace trimmed from both ends (the same set as the TypeScript twin, not str.strip's).
_WS = (
    " \t\n\r\f\v\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_HAS_WS = re.compile(f"[{re.escape(_WS)}]")
_QID_RE = re.compile(r"q([0-9]+)", re.I | re.A)
_WIKIDATA_ENTITY_RE = re.compile(
    r"(?:https?://)?(?:www\.)?wikidata\.org/(?:wiki|entity)/(q[0-9]+)/?", re.I | re.A
)
# "x.com/jack", "https://github.com/octocat/", "twitter.com/@jack?lang=en" (prefix match)
_HANDLE_URL_RE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/+([^/?#]+)", re.I | re.A
)
_SCHEME_RE = re.compile(r"([a-z][a-z0-9+.-]*)://", re.I | re.A)
_PORT_RE = re.compile(r"[0-9]*", re.A)


def _trim(s: str) -> str:
    return s.strip(_WS)


def _require(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    s = _trim(value)
    if not s:
        raise ValueError("empty value")
    return s


def qid(value: object) -> str:
    """``q42`` -> ``Q42``. Also accepts a Wikidata entity URL."""
    s = _require(value)
    m = _WIKIDATA_ENTITY_RE.fullmatch(s)
    if m:
        s = m.group(1)
    m = _QID_RE.fullmatch(s)
    if not m:
        raise ValueError(f"not a Wikidata QID: {value!r}")
    return "Q" + m.group(1)


def handle(value: object) -> str:
    """``https://x.com/Jack`` -> ``jack``; ``@Jack`` -> ``jack``.

    A URL gives its first path segment; leading ``@`` (and whitespace) are stripped. Anything
    else with a ``/`` is not a handle.
    """
    s = _require(value)
    m = _HANDLE_URL_RE.match(s)
    if m:
        s = m.group(1)
    s = _trim(s.lstrip("@" + _WS)).lower()
    if not s or "/" in s:
        raise ValueError(f"not a handle: {value!r}")
    return s


def _strip_www(host: str) -> str:
    """Drop leading ``www.`` labels while the rest still contains a dot (``www.com`` stays)."""
    while host.startswith("www.") and "." in host[4:]:
        host = host[4:]
    return host


def _authority(s: str) -> tuple[str, str, str]:
    """(scheme or "", authority without userinfo, rest) of a URL-ish string."""
    m = _SCHEME_RE.match(s)
    scheme = m.group(1).lower() if m else ""
    rest = s[m.end() :] if m else s
    end = len(rest)
    for ch in "/?#":
        i = rest.find(ch)
        if i != -1:
            end = min(end, i)
    authority, rest = rest[:end], rest[end:]
    return scheme, authority.rpartition("@")[2], rest


def _host_port(authority: str, value: object) -> tuple[str, str | None]:
    """Lowercased host (trailing dots and leading ``www.`` dropped) and port (digits, no leading
    zeros) of an authority. An IPv6 literal keeps its brackets."""
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise ValueError(f"bad IPv6 host: {value!r}")
        host, port_part = authority[: end + 1].lower(), authority[end + 1 :]
        if port_part and not port_part.startswith(":"):
            raise ValueError(f"bad host: {value!r}")
        port = port_part[1:] if port_part else None
    else:
        host, sep, port = authority.partition(":")
        host = _strip_www(host.lower().rstrip("."))
        if "[" in host or "]" in host:
            raise ValueError(f"bad host: {value!r}")
        if not sep:
            port = None
    if port is not None:
        if not _PORT_RE.fullmatch(port):
            raise ValueError(f"bad port: {value!r}")
        port = None if port == "" else str(int(port))
        if port is not None and int(port) > 65535:
            raise ValueError(f"bad port: {value!r}")
    if not host or _HAS_WS.search(host):
        raise ValueError(f"bad host: {value!r}")
    return host, port


def domain(value: object) -> str:
    """``https://www.Stripe.com/about`` -> ``stripe.com``; ``www.com`` stays ``www.com``."""
    s = _require(value)
    _, authority, _ = _authority(s)
    host, _ = _host_port(authority, value)
    if host.startswith("[") or "." not in host or "" in host.split("."):
        raise ValueError(f"not a domain: {value!r}")
    return host


def url(value: object) -> str:
    """``https://www.acme.ai/`` -> ``https://acme.ai`` (path case kept; query/fragment dropped).

    A missing scheme means ``https``; userinfo is dropped; the port is kept.
    """
    s = _require(value)
    scheme, authority, rest = _authority(s)
    host, port = _host_port(authority, value)
    end = len(rest)
    for ch in "?#":
        i = rest.find(ch)
        if i != -1:
            end = min(end, i)
    path = rest[:end].rstrip("/" + _WS)  # "/x /" -> "/x" (the result must be trimmed too)
    netloc = f"{host}:{port}" if port is not None else host
    return f"{scheme or 'https'}://{netloc}{path}"


def email(value: object) -> str:
    s = _require(value).lower()
    if "@" not in s:
        raise ValueError(f"not an email: {value!r}")
    return s


def exact(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)  # e.g. an HN story id given as a number
    return _require(value)


NORMALIZERS: dict[str, Callable[[object], str]] = {
    "qid": qid,
    "handle": handle,
    "domain": domain,
    "url": url,
    "email": email,
    "exact": exact,
}


def get_normalizer(name: str | None) -> Callable[[object], str]:
    """The normalizer named in ``predicate.meta.normalize`` (default ``exact``)."""
    try:
        return NORMALIZERS[name or "exact"]
    except KeyError:
        raise ValueError(f"unknown normalizer {name!r}") from None


def normalize_name(value: str) -> str:
    """Name key for the fuzzy pass: lowercase, strip punctuation and accents, collapse whitespace.

    ``"  Ada  Lovelace-Byron, Jr. "`` -> ``"ada lovelacebyron jr"``; ``"José"`` -> ``"jose"``.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    kept = "".join(
        ch
        for ch in decomposed
        if not unicodedata.combining(ch) and not unicodedata.category(ch).startswith("P")
    )
    return " ".join(kept.casefold().split())
