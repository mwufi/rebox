"""Shared fixtures.

Database: ``scripts/test-db.sh s2_pytest`` is run once per session (fresh, migrated, seeded).
Set ``S2_OWNER_URL`` + ``S2_APP_URL`` to reuse an existing database instead. The worker
always connects with the app URL (role rebox_app, RLS applies); the owner URL is only used
here to reset tables and to inspect results.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).parent))

from fake_writer import FakeWriter  # noqa: E402

from rebox_worker.client import ClaimsClient  # noqa: E402
from rebox_worker.connectors.base import Ctx  # noqa: E402
from rebox_worker.db import Db  # noqa: E402
from rebox_worker.pipeline import Deps  # noqa: E402
from rebox_worker.rawstore import FileRawStore  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
TEST_DB = os.environ.get("S2_TEST_DB", "s2_pytest")
TABLES = "claim, entity, change, job, connector_state, raw_record, resolution_log, review_item, merge_log"
SEEDED_LAYERS = ("public/manual", "public/wikidata", "public/hn", "public/github", "public/x")


@pytest.fixture(scope="session")
def db_urls() -> tuple[str, str]:
    if os.environ.get("S2_OWNER_URL") and os.environ.get("S2_APP_URL"):
        return os.environ["S2_OWNER_URL"], os.environ["S2_APP_URL"]
    assert TEST_DB.startswith("s2_"), "S2 tests only use s2_ databases"
    out = subprocess.run(
        [str(REPO / "scripts" / "test-db.sh"), TEST_DB], check=True, capture_output=True, text=True
    ).stdout.split()
    return out[0], out[1]


@pytest.fixture(scope="session")
def owner_url(db_urls: tuple[str, str]) -> str:
    return db_urls[0]


@pytest.fixture(scope="session")
def app_url(db_urls: tuple[str, str]) -> str:
    return db_urls[1]


@pytest.fixture
def owner(owner_url: str) -> Iterator[psycopg.Connection]:
    """Owner (superuser) connection for setup and inspection; superusers bypass RLS."""
    with psycopg.connect(owner_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def clean(owner_url: str) -> None:
    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute(f"truncate {TABLES} restart identity cascade")
        conn.execute("delete from layer where id <> all(%s)", (list(SEEDED_LAYERS),))


@pytest.fixture
def db(app_url: str, clean: None) -> Iterator[Db]:
    d = Db(app_url, max_size=4)
    yield d
    d.close()


@pytest.fixture
def writer(app_url: str) -> Iterator[FakeWriter]:
    w = FakeWriter(app_url).start()
    yield w
    w.stop()


@pytest.fixture
def client(writer: FakeWriter) -> Iterator[ClaimsClient]:
    c = ClaimsClient(writer.url, writer.token, backoff_s=0.01, max_backoff_s=0.05)
    yield c
    c.close()


@pytest.fixture
def deps(db: Db, client: ClaimsClient, tmp_path: Path, app_url: str) -> Deps:
    return Deps(
        db=db,
        raw=FileRawStore(tmp_path / "raw", db),
        client=client,
        ctx=Ctx(fixtures=REPO / "fixtures" / "raw"),
        lock_url=app_url,
    )


def q(conn: psycopg.Connection, sql: str, *args: object) -> list[dict]:
    return conn.execute(sql, args or None).fetchall()
