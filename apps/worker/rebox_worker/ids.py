"""Deterministic entity ids (CONTRACTS §3).

    id = "rbx:<kind>:" + base32_lower_nopad(sha256(f"{kind}|{key}|{normalized_value}"))[:20]

using the strongest identity key in ``IDENTITY_KEY_ORDER``. A company the resolver mints from a
name alone (its unscoped name pass) passes ``{"name": fold_name(kind, name)}``: ``name`` is the
last entry of the *mint* key order (``MINT_KEY_ORDER``) so later mentions of the same name
converge on one id. ``name`` is never an identity key anywhere else (``strongest_key`` ignores
it). With no key at all: ``sha256(f"{kind}|src|{source}|{source_ref}")``. Retries and replays
therefore never mint duplicates.
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
    "x_id",
    "hn_user",
    "hn_story",
    "url",
    "website",
    "email",
)


# Minting only: a folded company name, weaker than every identity key (CONTRACTS §3).
NAME_KEY = "name"
MINT_KEY_ORDER: tuple[str, ...] = (*IDENTITY_KEY_ORDER, NAME_KEY)


def _digest_id(material: str) -> str:
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:20]


def strongest_key(identity: Mapping[str, str | list[str]]) -> tuple[str, str] | None:
    """(predicate, value) of the strongest identity key present, or None.

    For multi-valued keys the smallest value is used, so the choice is order independent.
    ``name`` is not an identity key and is ignored here (see ``mint_id``).
    """
    return _first_key(identity, IDENTITY_KEY_ORDER)


def _first_key(identity: Mapping[str, str | list[str]], order: tuple[str, ...]) -> tuple[str, str] | None:
    for key in order:
        value = identity.get(key)
        if isinstance(value, list):
            value = min(value) if value else None
        if value:
            return key, value
    return None


def mint_id(kind: str, identity: Mapping[str, str | list[str]], source: str, source_ref: str) -> str:
    """Deterministic id for a new entity. ``identity`` values must already be normalized
    (``name``: folded with ``names.fold_name``)."""
    strongest = _first_key(identity, MINT_KEY_ORDER)
    if strongest is not None:
        key, value = strongest
        material = f"{kind}|{key}|{value}"
    else:
        material = f"{kind}|src|{source}|{source_ref}"
    return f"rbx:{kind}:{_digest_id(material)}"
