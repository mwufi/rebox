"""Deterministic entity ids (CONTRACTS §3).

    id = "rbx:<kind>:" + base32_lower_nopad(sha256(f"{kind}|{key}|{normalized_value}"))[:20]

using the strongest identity key in ``IDENTITY_KEY_ORDER``. With no identity key:
``sha256(f"{kind}|src|{source}|{source_ref}")``. Retries and replays therefore never
mint duplicates.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping

IDENTITY_KEY_ORDER: tuple[str, ...] = (
    "wikidata",
    "domain",
    "github",
    "x_handle",
    "hn_user",
    "hn_story",
    "url",
    "website",
    "email",
)


def _digest_id(material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:20]


def strongest_key(identity: Mapping[str, str | list[str]]) -> tuple[str, str] | None:
    """(predicate, value) of the strongest identity key present, or None.

    For multi-valued keys the smallest value is used, so the choice is order independent.
    """
    for key in IDENTITY_KEY_ORDER:
        value = identity.get(key)
        if isinstance(value, list):
            value = min(value) if value else None
        if value:
            return key, value
    return None


def mint_id(kind: str, identity: Mapping[str, str | list[str]], source: str, source_ref: str) -> str:
    """Deterministic id for a new entity. ``identity`` values must already be normalized."""
    strongest = strongest_key(identity)
    if strongest is not None:
        key, value = strongest
        material = f"{kind}|{key}|{value}"
    else:
        material = f"{kind}|src|{source}|{source_ref}"
    return f"rbx:{kind}:{_digest_id(material)}"
