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


# ── S16: raw reindex ──────────────────────────────────────────────────────


def _index_rows(owner, source="hn"):
    rows = owner.execute(
        "select external_id, store_key, fetched_at from raw_record where source = %s order by external_id",
        (source,),
    ).fetchall()
    return [(r["external_id"], r["store_key"], r["fetched_at"]) for r in rows]


def _crashed_run(store: RawStore) -> dict[str, str]:
    """A store as an aborted sync leaves it: some records indexed, others only written."""
    keys = {}
    keys["indexed"] = store.put(rec({"n": 1}, ext="story:1")).key
    keys["unindexed"] = store.put(rec({"n": 2}, ext="story:2"), index=False).key
    # story:3: an old indexed version, and a newer version that was written but not indexed
    keys["old"] = store.put(rec({"n": 3}, ext="story:3")).key
    later = datetime(2026, 11, 5, tzinfo=UTC)
    keys["new"] = store.put(rec({"n": 33}, ext="story:3", when=later), index=False).key
    # story:4: two unindexed versions; the newest wins
    store.put(rec({"n": 4}, ext="story:4"), index=False)
    keys["newest4"] = store.put(rec({"n": 44}, ext="story:4", when=later), index=False).key
    # another source in the same store is never touched
    store.put(RawRecord.new("x", "user:1", {"n": 5}, fetched_at=WHEN), index=False)
    return keys


def _check_reindex(store: RawStore, owner) -> None:
    keys = _crashed_run(store)
    assert [r.external_id for r in store.iter("hn")] == ["story:1", "story:3"]  # replay misses 2 and 4

    dry = store.reindex("hn", dry_run=True)
    assert (dry.scanned, dry.missing, dry.stale, dry.up_to_date, dry.superseded, dry.invalid) == (
        6,
        2,
        1,
        1,
        2,
        0,
    )
    assert dry.dry_run and dry.indexed == 3
    assert [r[0] for r in _index_rows(owner)] == ["story:1", "story:3"]  # dry run wrote nothing

    stats = store.reindex("hn")
    assert (stats.missing, stats.stale, stats.up_to_date, stats.indexed) == (2, 1, 1, 3)
    assert [(ext, key) for ext, key, _ in _index_rows(owner)] == [
        ("story:1", keys["indexed"]),
        ("story:2", keys["unindexed"]),
        ("story:3", keys["new"]),
        ("story:4", keys["newest4"]),
    ]
    assert [r.payload["n"] for r in store.iter("hn")] == [1, 2, 33, 44]  # replay sees everything now
    assert _index_rows(owner, "x") == []

    # Idempotent: a second run changes nothing.
    before = _index_rows(owner)
    again = store.reindex("hn")
    assert (again.missing, again.stale, again.up_to_date, again.indexed) == (0, 0, 4, 0)
    assert _index_rows(owner) == before
    # And put() now skips the reindexed records as unchanged.
    assert store.put(rec({"n": 2}, ext="story:2")).skipped


def test_reindex_file_store(db, owner, tmp_path):
    store = FileRawStore(tmp_path / "raw", db)
    _check_reindex(store, owner)


def test_reindex_s3_store(db, owner, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket="rebox-raw", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
        )
        client.put_object(Bucket="rebox-raw", Key="other/hn/2026/11/01/x.json", Body=b"{}")  # outside prefix
        store = S3RawStore("rebox-raw", "raw/", db, client=client)
        _check_reindex(store, owner)


def test_s3_list_keys_pages(db, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-west-2")
        client.create_bucket(
            Bucket="rebox-raw", CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
        )
        for i in range(1105):  # more than one list_objects_v2 page (1000)
            client.put_object(Bucket="rebox-raw", Key=f"raw/hn/k{i:05}.json", Body=b"{}")
        store = S3RawStore("rebox-raw", "raw", db, client=client)
        keys = list(store.list_keys("hn/"))
    assert len(keys) == 1105 and keys[0] == "hn/k00000.json"


def test_reindex_skips_invalid_and_temp_files(db, owner, tmp_path):
    store = FileRawStore(tmp_path / "raw", db)
    store.put(rec({"n": 1}, ext="story:1"), index=False)
    bad = tmp_path / "raw" / "hn" / "2026" / "11" / "01"
    (bad / "broken.json").write_text("{not json")
    (bad / "other.json").write_text(RawRecord.new("x", "u", {}, fetched_at=WHEN).to_json())
    (bad / ".tmp-abc").write_text("partial")
    (bad / "notes.txt").write_text("hi")
    stats = store.reindex("hn")
    assert (stats.scanned, stats.invalid, stats.missing) == (3, 2, 1)
    assert [r[0] for r in _index_rows(owner)] == ["story:1"]
    assert store.reindex("nothing-here").scanned == 0
    with pytest.raises(ValueError):
        store.reindex("../etc")


def test_reindex_never_moves_the_index_back(db, owner, tmp_path):
    """A sync that indexes a newer version while reindex runs is not overwritten by it."""
    store = FileRawStore(tmp_path / "raw", db)
    store.put(rec({"n": 1}, ext="story:1"), index=False)
    newer = store.put(rec({"n": 2}, ext="story:1", when=datetime(2026, 12, 1, tzinfo=UTC))).key
    assert store.reindex("hn").up_to_date == 1
    assert _index_rows(owner)[0][1] == newer


def test_cli_raw_reindex(db, owner, tmp_path, app_url, monkeypatch, capsys):
    from rebox_worker.cli import main

    store = FileRawStore(tmp_path / "raw", db)
    store.put(rec({"n": 1}, ext="story:1"), index=False)
    monkeypatch.setenv("DATABASE_URL", app_url)
    monkeypatch.setenv("RAW_STORE", f"file://{tmp_path / 'raw'}")
    assert main(["raw", "reindex", "hn", "--dry-run"]) == 0
    assert '"missing": 1' in capsys.readouterr().out and _index_rows(owner) == []
    assert main(["raw", "reindex", "hn"]) == 0
    out = capsys.readouterr().out
    assert '"indexed": 1' in out and "rebox-worker replay hn" in out
    assert [r[0] for r in _index_rows(owner)] == ["story:1"]
