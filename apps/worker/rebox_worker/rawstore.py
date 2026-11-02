"""Raw store: every fetched upstream document, kept verbatim for replay (CONTRACTS §8).

Key layout: ``{source}/{yyyy}/{mm}/{dd}/{sha256(external_id)[:16]}.json`` (date = fetched_at, UTC).
The ``raw_record`` table indexes the latest stored version of each (source, external_id);
a re-fetch with the same ``content_hash`` is skipped.

The pipeline separates the two steps of ``put`` (``put(rec, index=False)`` then ``index()``),
so a record is only marked as seen after its claims were written. Otherwise a failed write
would make the retry skip the record as "unchanged" and its claims would be lost.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from .db import Db
from .models import RawRecord


@dataclass(frozen=True)
class PutResult:
    key: str
    skipped: bool  # content_hash unchanged since the last indexed version; nothing written


def store_key(rec: RawRecord) -> str:
    d = rec.fetched_at.astimezone(UTC)
    h = hashlib.sha256(rec.external_id.encode("utf-8")).hexdigest()[:16]
    return f"{rec.source}/{d:%Y}/{d:%m}/{d:%d}/{h}.json"


class RawStore(ABC):
    def __init__(self, db: Db | None = None) -> None:
        self.db = db

    @staticmethod
    def from_url(url: str, db: Db | None = None, **kwargs: Any) -> RawStore:
        """``file:///abs/path`` (or ``file://./rel``) or ``s3://bucket/prefix``."""
        if url.startswith("file://"):
            return FileRawStore(Path(url.removeprefix("file://")), db)
        if url.startswith("s3://"):
            bucket, _, prefix = url.removeprefix("s3://").partition("/")
            if not bucket:
                raise ValueError(f"RAW_STORE needs a bucket: {url!r}")
            return S3RawStore(bucket, prefix, db, **kwargs)
        raise ValueError(f"unsupported RAW_STORE {url!r} (use file:// or s3://)")

    # ── storage backend ──
    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    # ── records ──
    def put(self, rec: RawRecord, *, index: bool = True) -> PutResult:
        """Store ``rec`` unless its content is unchanged; return the key."""
        known = self._indexed(rec.source, rec.external_id)
        if known and known[1] == rec.content_hash:
            return PutResult(known[0], skipped=True)
        key = store_key(rec)
        self.write_bytes(key, rec.to_json().encode("utf-8"))
        if index:
            self.index(rec, key)
        return PutResult(key, skipped=False)

    def get(self, key: str) -> RawRecord:
        return RawRecord.model_validate_json(self.read_bytes(key))

    def index(self, rec: RawRecord, key: str) -> None:
        """Record ``key`` as the latest version of ``rec`` in ``raw_record``."""
        if self.db is None:
            return
        with self.db.view() as conn:
            conn.execute(
                """insert into raw_record (source, external_id, content_hash, store_key, fetched_at)
                   values (%s, %s, %s, %s, %s)
                   on conflict (source, external_id) do update
                     set content_hash = excluded.content_hash, store_key = excluded.store_key,
                         fetched_at = excluded.fetched_at""",
                (rec.source, rec.external_id, rec.content_hash, key, rec.fetched_at),
            )

    def iter(self, source: str) -> Iterator[RawRecord]:
        """The latest stored version of every record of ``source``, in external_id order."""
        if self.db is None:
            raise RuntimeError("iter() needs a database (it reads the raw_record index)")
        with self.db.view() as conn:
            keys = [
                r["store_key"]
                for r in conn.execute(
                    "select store_key from raw_record where source = %s order by external_id", (source,)
                )
            ]
        for key in keys:
            yield self.get(key)

    def _indexed(self, source: str, external_id: str) -> tuple[str, str] | None:
        if self.db is None:
            return None
        with self.db.view() as conn:
            row = conn.execute(
                "select store_key, content_hash from raw_record where source = %s and external_id = %s",
                (source, external_id),
            ).fetchone()
        return (row["store_key"], row["content_hash"]) if row else None


class FileRawStore(RawStore):
    def __init__(self, root: Path, db: Db | None = None) -> None:
        super().__init__(db)
        self.root = root.expanduser().resolve()

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)  # atomic: readers never see a half-written file
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read_bytes(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class S3RawStore(RawStore):
    def __init__(self, bucket: str, prefix: str = "", db: Db | None = None, *, client: Any = None) -> None:
        super().__init__(db)
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or None)
        self.client = client

    def _object_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def write_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self._object_key(key), Body=data, ContentType="application/json"
        )

    def read_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))["Body"].read()
