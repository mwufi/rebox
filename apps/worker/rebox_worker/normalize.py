"""Identity value normalizers (CONTRACTS §4) and name normalization for fuzzy matching.

Identity values are stored normalized, so the resolver can match them with a plain
equality lookup. Every normalizer is idempotent: normalize(normalize(x)) == normalize(x).
Normalizers raise ``ValueError`` for values that cannot be normalized.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from urllib.parse import urlsplit

_QID_RE = re.compile(r"^Q\d+$")
_WIKIDATA_ENTITY_RE = re.compile(r"^(?:https?://)?(?:www\.)?wikidata\.org/(?:wiki|entity)/(Q\d+)/?$", re.I)
# "x.com/jack", "https://github.com/octocat/", "twitter.com/@jack?lang=en"
_HANDLE_URL_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/+([^/?#]+)", re.I
)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


def _require(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    s = value.strip()
    if not s:
        raise ValueError("empty value")
    return s


def qid(value: object) -> str:
    """``q42`` -> ``Q42``. Also accepts a Wikidata entity URL."""
    s = _require(value)
    m = _WIKIDATA_ENTITY_RE.match(s)
    if m:
        s = m.group(1)
    s = s.upper()
    if not _QID_RE.match(s):
        raise ValueError(f"not a Wikidata QID: {value!r}")
    return s


def handle(value: object) -> str:
    """``https://x.com/Jack`` -> ``jack``; ``@Jack`` -> ``jack``."""
    s = _require(value)
    m = _HANDLE_URL_RE.match(s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip().lower()
    if not s:
        raise ValueError(f"not a handle: {value!r}")
    return s


def _split(value: str):
    """urlsplit that tolerates a missing scheme ("www.acme.ai/x")."""
    return urlsplit(value if _SCHEME_RE.match(value) else f"https://{value}")


def domain(value: object) -> str:
    """``https://www.Stripe.com/about`` -> ``stripe.com``."""
    s = _require(value)
    host = (_split(s).hostname or "").rstrip(".")  # hostname drops userinfo + port, lowercases
    host = host.removeprefix("www.")
    if not host or "." not in host:
        raise ValueError(f"not a domain: {value!r}")
    return host


def url(value: object) -> str:
    """``https://www.acme.ai/`` -> ``https://acme.ai`` (path case kept; query/fragment dropped)."""
    s = _require(value)
    parts = _split(s)
    host = (parts.hostname or "").rstrip(".").removeprefix("www.")
    if not host:
        raise ValueError(f"not a URL: {value!r}")
    netloc = f"{host}:{parts.port}" if parts.port else host
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{netloc}{path}"


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
