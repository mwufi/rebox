import hashlib
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from rebox_worker.models import RawRecord
from rebox_worker.rawstore import FileRawStore, RawStore, S3RawStore, store_key

WHEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def rec(payload, ext="story:41234567", when=WHEN):
    return RawRecord.new("hn", ext, payload, url="https://news.ycombinator.com/item?id=1", fetched_at=when)


def test_store_key_layout():
    h = hashlib.sha256(b"story:41234567").hexdigest()[:16]
    assert store_key(rec({"a": 1})) == f"hn/2026/11/01/{h}.json"


def test_content_hash_is_canonical():
    assert rec({"a": 1, "b": [1, 2]}).content_hash == rec({"b": [1, 2], "a": 1}).content_hash
    assert rec({"a": 1}).content_hash != rec({"a": 2}).content_hash


def test_from_url(tmp_path):
    assert isinstance(RawStore.from_url(f"file://{tmp_path}"), FileRawStore)
    s3 = RawStore.from_url("s3://bucket/some/prefix", client=object())
    assert isinstance(s3, S3RawStore) and s3.bucket == "bucket" and s3.prefix == "some/prefix"
    with pytest.raises(ValueError):
        RawStore.from_url("gs://nope")


def _exercise(store: RawStore, owner) -> None:
    first = store.put(rec({"title": "Launch HN: Acme"}))
    assert not first.skipped
    again = store.put(rec({"title": "Launch HN: Acme"}, when=datetime(2026, 11, 2, tzinfo=UTC)))
    assert again.skipped and again.key == first.key  # unchanged content -> nothing written
    changed = store.put(rec({"title": "Launch HN: Acme (YC W24)"}, when=datetime(2026, 11, 3, tzinfo=UTC)))
    assert not changed.skipped and changed.key != first.key and changed.key.startswith("hn/2026/11/03/")
    store.put(rec({"title": "other"}, ext="story:2"))

    rows = owner.execute("select external_id, store_key from raw_record order by external_id").fetchall()
    assert [(r["external_id"], r["store_key"]) for r in rows] == [
        ("story:2", store_key(rec({}, ext="story:2"))),
        ("story:41234567", changed.key),
    ]
    replayed = list(store.iter("hn"))
    assert [r.external_id for r in replayed] == ["story:2", "story:41234567"]
    assert replayed[1].payload == {"title": "Launch HN: Acme (YC W24)"}
    assert replayed[1] == store.get(changed.key)


def test_file_store_skip_unchanged(db, owner, tmp_path):
    store = FileRawStore(tmp_path / "raw", db)
    _exercise(store, owner)
    assert len(list((tmp_path / "raw").rglob("*.json"))) == 3  # the skipped put wrote no file
    assert not list((tmp_path / "raw").rglob(".tmp-*"))


def test_s3_store_skip_unchanged(db, owner, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket="rebox-raw", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
        )
        store = S3RawStore("rebox-raw", "raw/", db, client=client)
        _exercise(store, owner)
        keys = sorted(o["Key"] for o in client.list_objects_v2(Bucket="rebox-raw")["Contents"])
        assert len(keys) == 3 and all(k.startswith("raw/hn/2026/11/") for k in keys)


def test_put_without_index_does_not_mark_seen(db, tmp_path):
    store = FileRawStore(tmp_path / "raw", db)
    r = rec({"x": 1})
    res = store.put(r, index=False)
    assert not store.put(r, index=False).skipped  # not indexed yet -> not skipped
    store.index(r, res.key)
    assert store.put(r).skipped
