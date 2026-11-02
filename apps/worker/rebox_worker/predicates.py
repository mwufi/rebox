"""The predicate vocabulary, loaded from the ``predicate`` table (CONTRACTS §1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from .normalize import get_normalizer


@dataclass(frozen=True)
class Predicate:
    id: str
    subject_kinds: tuple[str, ...]
    object_kind: str | None
    cardinality: str
    meta: dict[str, Any]

    @property
    def is_edge(self) -> bool:
        return self.object_kind is not None

    @property
    def is_identity(self) -> bool:
        return bool(self.meta.get("identity"))

    @property
    def is_private(self) -> bool:
        return bool(self.meta.get("private"))

    @property
    def normalizer(self) -> Callable[[object], str]:
        return get_normalizer(self.meta.get("normalize"))


def load_predicates(conn: psycopg.Connection) -> dict[str, Predicate]:
    rows = conn.execute("select id, subject_kinds, object_kind, cardinality, meta from predicate").fetchall()
    out: dict[str, Predicate] = {}
    for r in rows:
        r = (
            r
            if isinstance(r, dict)
            else dict(zip(("id", "subject_kinds", "object_kind", "cardinality", "meta"), r, strict=True))
        )
        out[r["id"]] = Predicate(
            id=r["id"],
            subject_kinds=tuple(r["subject_kinds"]),
            object_kind=r["object_kind"],
            cardinality=r["cardinality"],
            meta=dict(r["meta"] or {}),
        )
    return out
