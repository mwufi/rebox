"""Worker data models (CONTRACTS §5 and §8).

Connectors produce ``RawRecord`` / ``CursorUpdate`` (fetch) and ``ClaimInput`` (extract).
The resolver turns ``ClaimInput`` into ``ClaimWrite``, the exact request shape of
``POST /internal/claims``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IdentityValue = str | int | list[str | int]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def content_hash(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


# ── fetch side ────────────────────────────────────────────────────────────


class RawRecord(BaseModel):
    """One upstream document, stored verbatim in the raw store."""

    model_config = ConfigDict(extra="forbid")

    source: str
    external_id: str
    url: str | None = None
    fetched_at: datetime
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    payload: Any

    @classmethod
    def new(
        cls,
        source: str,
        external_id: str,
        payload: Any,
        *,
        url: str | None = None,
        fetched_at: datetime | None = None,
    ) -> RawRecord:
        """Build a record, hashing the payload (canonical JSON) so unchanged re-fetches are skipped."""
        return cls(
            source=source,
            external_id=external_id,
            url=url,
            fetched_at=fetched_at or utcnow(),
            content_hash=content_hash(payload),
            payload=payload,
        )

    @field_validator("fetched_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=UTC)

    def to_json(self) -> str:
        data = self.model_dump(mode="json")
        data["fetched_at"] = self.fetched_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)


class CursorUpdate(BaseModel):
    """Yielded by ``fetch``: everything yielded before it is covered by ``cursor``.

    The pipeline writes all pending claims, then persists the cursor.
    """

    model_config = ConfigDict(extra="forbid")

    cursor: dict[str, Any]


# ── extract side (before resolution) ──────────────────────────────────────


class EntityRef(BaseModel):
    """Either a known id (``EntityRef(id=...)``) or a description the resolver matches or mints.

    ``identity`` maps identity predicates to raw (un-normalized) values; ``context`` lists
    related entities (e.g. the company a founder belongs to) for the scoped fuzzy pass.
    """

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    kind: str | None = None
    identity: dict[str, IdentityValue] = Field(default_factory=dict)
    name: str | None = None
    context: list[EntityRef] | None = None

    @field_validator("identity", mode="before")
    @classmethod
    def _drop_empty(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: val for k, val in v.items() if val not in (None, "", [])}
        return v

    @model_validator(mode="after")
    def _one_form(self) -> EntityRef:
        if self.id is not None:
            if self.kind or self.identity or self.name or self.context:
                raise ValueError("EntityRef with id must not carry kind/identity/name/context")
        elif not self.kind:
            raise ValueError("EntityRef needs either id or kind")
        return self


class ClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: EntityRef
    predicate: str
    object: EntityRef | None = None
    value: Any = None
    valid_from: str | None = None
    valid_to: str | None = None
    retract: bool = False
    source_ref: str = Field(min_length=1)


# ── resolved (writer request shape, CONTRACTS §5) ─────────────────────────


class MintSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^(rbx|local):[a-z]+:[a-z0-9-]+$")
    kind: str


class IdRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class MintRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mint: MintSpec


WriteRef = IdRef | MintRef


class ClaimWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: WriteRef
    predicate: str
    object: WriteRef | None = None
    value: Any = None
    valid_from: str | None = None
    valid_to: str | None = None
    retract: bool = False
    source_ref: str

    @property
    def subject_id(self) -> str:
        return self.subject.id if isinstance(self.subject, IdRef) else self.subject.mint.id

    @property
    def object_id(self) -> str | None:
        if self.object is None:
            return None
        return self.object.id if isinstance(self.object, IdRef) else self.object.mint.id

    def to_request(self) -> dict[str, Any]:
        """JSON for one element of ``claims`` in ``POST /internal/claims`` (optional keys omitted)."""
        out: dict[str, Any] = {
            "subject": self.subject.model_dump(),
            "predicate": self.predicate,
            "source_ref": self.source_ref,
        }
        if self.object is not None:
            out["object"] = self.object.model_dump()
        if self.value is not None:
            out["value"] = self.value
        if self.valid_from is not None:
            out["valid_from"] = self.valid_from
        if self.valid_to is not None:
            out["valid_to"] = self.valid_to
        if self.retract:
            out["retract"] = True
        return out


Decision = Literal["matched", "minted", "review"]
