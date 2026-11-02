"""Raw store: every fetched upstream document, kept verbatim for replay (CONTRACTS §8).

Key layout: ``{source}/{yyyy}/{mm}/{dd}/{sha256(external_id)[:16]}.json`` (date = fetched_at, UTC).
The ``raw_record`` table indexes the latest stored version of each (source, external_id);
a re-fetch with the same ``content_hash`` is skipped.

The pipeline separates the two steps of ``put`` (``put(rec, index=False)`` then ``index()``),
so a record is only marked as seen after its claims were written. Otherwise a failed write
would make the retry skip the record as "unchanged" and its claims would be lost.

The flip side: a run that aborts between the two steps leaves files in the store that the
index (and so ``replay``) does not know. ``reindex(source)`` (``rebox-worker raw reindex
SOURCE [--dry-run]``) walks the store and indexes every record that is missing from
``raw_record`` or newer than its indexed version; run ``replay SOURCE`` afterwards to extract
them. It is idempotent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .db import Db
from .models import RawRecord

log = logging.getLogger(__name__)

SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class PutResult:
    key: str
    skipped: bool  # content_hash unchanged since the last indexed version; nothing written


@dataclass
class ReindexStats:
    """``reindex`` counts. ``scanned`` counts files; the rest count records (external ids)."""

    source: str
    dry_run: bool = False
    scanned: int = 0  # record files found under {source}/
    invalid: int = 0  # unreadable, not a RawRecord, or a record of another source
    superseded: int = 0  # older stored versions of a record that has a newer file
    up_to_date: int = 0  # index already points at the newest stored version
    missing: int = 0  # not in raw_record: indexed now (or would be, with dry_run)
    stale: int = 0  # indexed version older than a stored one: re-pointed (or would be)

    @property
    def indexed(self) -> int:
        return self.missing + self.stale


@dataclass(frozen=True)
class _Version:
    key: str
    fetched_at: datetime
    content_hash: str


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

    @abstractmethod
    def list_keys(self, prefix: str) -> Iterator[str]:
        """Every stored key that starts with ``prefix`` (store-relative, like ``store_key``)."""

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

    def reindex(self, source: str, *, dry_run: bool = False) -> ReindexStats:
        """Index every stored record of ``source`` that ``raw_record`` lacks or has an older
        version of (by ``fetched_at``). Never moves the index back to an older version, so it
        is idempotent and safe next to a running sync. ``dry_run`` only counts."""
        if self.db is None:
            raise RuntimeError("reindex() needs a database (it writes the raw_record index)")
        if not SOURCE_RE.match(source):
            raise ValueError(f"not a source name: {source!r}")
        stats = ReindexStats(source=source, dry_run=dry_run)
        newest: dict[str, _Version] = {}
        for key in self.list_keys(f"{source}/"):
            if not key.endswith(".json") or key.rsplit("/", 1)[-1].startswith(".tmp-"):
                continue  # not a record (e.g. an interrupted FileRawStore write)
            stats.scanned += 1
            try:
                rec = self.get(key)
            except (OSError, ValueError, ValidationError) as e:
                stats.invalid += 1
                log.warning("raw reindex %s: unreadable record %s: %s", source, key, e)
                continue
            if rec.source != source:
                stats.invalid += 1
                log.warning("raw reindex %s: %s holds a record of source %r", source, key, rec.source)
                continue
            seen = newest.get(rec.external_id)
            if seen is None or (rec.fetched_at, key) > (seen.fetched_at, seen.key):
                newest[rec.external_id] = _Version(key, rec.fetched_at, rec.content_hash)
            if seen is not None:
                stats.superseded += 1
        with self.db.view() as conn:
            indexed = {
                r["external_id"]: r["fetched_at"]
                for r in conn.execute(
                    "select external_id, fetched_at from raw_record where source = %s", (source,)
                )
            }
            todo: list[tuple[str, _Version]] = []
            for ext, v in sorted(newest.items()):
                at = indexed.get(ext)
                if at is None:
                    stats.missing += 1
                elif at < v.fetched_at:
                    stats.stale += 1
                else:
                    stats.up_to_date += 1
                    continue
                todo.append((ext, v))
            if todo and not dry_run:
                with conn.cursor() as cur:
                    cur.executemany(
                        """insert into raw_record (source, external_id, content_hash, store_key, fetched_at)
                           values (%s, %s, %s, %s, %s)
                           on conflict (source, external_id) do update
                             set content_hash = excluded.content_hash, store_key = excluded.store_key,
                                 fetched_at = excluded.fetched_at
                           where raw_record.fetched_at < excluded.fetched_at""",
                        [(source, ext, v.content_hash, v.key, v.fetched_at) for ext, v in todo],
                    )
        return stats

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

    def list_keys(self, prefix: str) -> Iterator[str]:
        base = self.root / prefix
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file():
                yield path.relative_to(self.root).as_posix()


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

    def list_keys(self, prefix: str) -> Iterator[str]:
        strip = f"{self.prefix}/" if self.prefix else ""
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": self._object_key(prefix)}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or ():
                yield obj["Key"].removeprefix(strip)
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")


# ── CLI: rebox-worker raw reindex SOURCE [--dry-run] ──────────────────────


def cmd_reindex(args: argparse.Namespace, settings: Any) -> int:
    with Db(settings.database_url, max_size=1) as db:
        stats = RawStore.from_url(settings.raw_store, db).reindex(args.source, dry_run=args.dry_run)
    out = {**asdict(stats), "indexed": stats.indexed}
    print(f"raw reindex {args.source}: " + json.dumps(out))
    if stats.indexed and not args.dry_run:
        print(f"run `rebox-worker replay {args.source}` to extract the {stats.indexed} newly indexed records")
    return 0


def add_parser(sub: Any) -> None:
    r = sub.add_parser("raw", help="raw store maintenance")
    rsub = r.add_subparsers(dest="raw_command", required=True, metavar="subcommand")
    ri = rsub.add_parser("reindex", help="index stored records missing from (or newer than) raw_record")
    ri.add_argument("source")
    ri.add_argument("--dry-run", action="store_true", help="only count what would be indexed")
    ri.set_defaults(func=cmd_reindex)
