"""Source trust (S17, CONTRACTS §12): the trust table and how registration applies it.

The table (``config/trust.yaml``) gives a worker its trust by source, else by kind. The client
applies it with ``PATCH /v0/admin/workers/:id`` only while the worker's trust is table-managed
(its kind default, or what the table set before) and only with an admin token.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from test_provenance import real_api  # noqa: F401 - module fixture: the real API on the test DB

from rebox_worker.client import DEFAULT_TRUST, ClaimsClient
from rebox_worker.identity import (
    BUILTIN_SOURCE_TRUST,
    TrustTable,
    WorkerSpec,
    load_trust,
    register,
    trust_for,
)

REPO = Path(__file__).resolve().parents[3]
ADMIN = "test-admin-token"


# ── the table ──


def test_trust_file_matches_builtin_copy_and_migration_0008():
    t = load_trust(path=REPO / "config" / "trust.yaml")
    assert (
        dict(t.defaults)
        == DEFAULT_TRUST
        == {
            "connector": 0.8,
            "spider": 0.8,
            "parser": 0.6,
            "enricher": 0.5,
            "external": 0.3,
        }
    )
    assert (
        dict(t.sources)
        == BUILTIN_SOURCE_TRUST
        == {
            "wikidata": 0.9,
            "github": 0.8,
            "hn": 0.7,
            "x_spider": 0.75,
            "exa": 0.7,
        }
    )
    sql = (REPO / "db" / "migrations" / "0008_legacy_workers.sql").read_text()
    inlined = {k: float(v) for k, v in re.findall(r"when '(\w+)'\s+then ([0-9.]+)", sql)}
    assert inlined == BUILTIN_SOURCE_TRUST
    assert (
        float(re.search(r"else ([0-9.]+)\s+-- defaults\.connector", sql).group(1)) == t.defaults["connector"]
    )


def test_trust_for_source_then_kind():
    assert trust_for("connector", "wikidata") == 0.9
    assert trust_for("connector", "hn") == 0.7
    assert trust_for("spider", "x_spider") == 0.75  # the seed-set spiders too
    assert trust_for("spider", "exa") == 0.7
    assert trust_for("parser", "x_parse") == 0.6 and trust_for("parser", "exa_parse") == 0.6
    assert trust_for("connector", "somewhere") == 0.8
    assert trust_for("enricher", "somewhere") == 0.5
    assert TrustTable().trust_for("connector", "wikidata") == 0.9  # built-in copy (no file)


def test_trust_file_override_and_validation(tmp_path):
    f = tmp_path / "t.yaml"
    f.write_text("defaults:\n  parser: 0.55\nsources:\n  hn: 0.65  # handles\n")
    t = load_trust(env={"TRUST_FILE": str(f)})
    assert (t.trust_for("connector", "hn"), t.trust_for("parser", "x"), t.trust_for("spider", "x")) == (
        0.65,
        0.55,
        0.8,
    )
    f.write_text("sources:\n  hn: 1.5\n")
    with pytest.raises(ValueError, match="between 0 and 1"):
        load_trust(path=f)
    f.write_text("sources:\n  hn: high\n")
    with pytest.raises(ValueError, match="must be a number"):
        load_trust(path=f)
    with pytest.raises(FileNotFoundError):
        load_trust(env={"TRUST_FILE": str(tmp_path / "missing.yaml")})


# ── registration (fake writer: the real worker table, POST/GET/PATCH like the API) ──


@pytest.fixture
def admin_client(writer) -> Iterator[ClaimsClient]:
    c = ClaimsClient(writer.url, writer.token, backoff_s=0.01, max_backoff_s=0.05, admin_token=ADMIN)
    yield c
    c.close()


@pytest.fixture
def plain_client(writer) -> Iterator[ClaimsClient]:
    c = ClaimsClient(writer.url, writer.token, backoff_s=0.01, max_backoff_s=0.05, admin_token="")
    yield c
    c.close()


def _worker(owner, wid: str) -> dict:
    return owner.execute("select trust, config from worker where id = %s", (wid,)).fetchone()


def _patches(writer) -> list[tuple[str, dict]]:
    return [(p, b) for m, p, b in writer.calls if m == "PATCH" and p.startswith("/v0/admin/")]


def _reg(client: ClaimsClient, trust: float | None, name: str = "wikidata", **kw) -> str:
    return client.register_worker(
        name=name, kind="connector", source=name, layers=[f"public/{name}"], trust=trust, **kw
    )


def test_new_worker_gets_its_table_trust(db, owner, writer, admin_client):
    wid = _reg(admin_client, 0.9)
    w = _worker(owner, wid)
    assert w["trust"] == pytest.approx(0.9)
    assert w["config"] == {"trust_source": "table", "trust_table": 0.9}
    assert _patches(writer) == [(f"/v0/admin/workers/{wid}", {"trust": 0.9})]
    # Same again: already at the table value, nothing to do.
    assert _reg(admin_client, 0.9) == wid
    assert len(_patches(writer)) == 1


def test_trust_equal_to_the_kind_default_needs_nothing(db, owner, writer, plain_client, caplog):
    caplog.set_level(logging.INFO, logger="rebox_worker.client")
    wid = _reg(plain_client, 0.8, name="github")
    assert _worker(owner, wid) == {"trust": pytest.approx(0.8), "config": {}}
    _reg(plain_client, 0.8, name="github")
    assert not _patches(writer) and "ADMIN_TOKEN" not in caplog.text


def test_existing_default_worker_is_patched_to_the_table(db, owner, writer, admin_client):
    wid = _reg(admin_client, None, name="hn")  # registered before the table existed
    assert _worker(owner, wid)["trust"] == pytest.approx(0.8)
    assert _reg(admin_client, 0.7, name="hn") == wid
    assert _worker(owner, wid)["trust"] == pytest.approx(0.7)
    # Registering again (the next sync of the same process) does not read the worker again.
    _reg(admin_client, 0.7, name="hn")
    gets = [p for m, p, _ in writer.calls if m == "GET"]
    assert gets == [f"/v0/workers/{wid}"]


def test_admin_changed_trust_is_never_touched(db, owner, writer, admin_client):
    wid = _reg(admin_client, None, name="hn")
    owner.execute("update worker set trust = 0.42 where id = %s", (wid,))  # an admin's PATCH
    _reg(admin_client, 0.7, name="hn")
    assert _worker(owner, wid)["trust"] == pytest.approx(0.42) and not _patches(writer)
    # Also when the table set it once and an admin changed it afterwards.
    wid2 = _reg(admin_client, 0.9)
    owner.execute("update worker set trust = 0.5 where id = %s", (wid2,))
    _reg(admin_client, 0.95)
    assert _worker(owner, wid2)["trust"] == pytest.approx(0.5)


def test_a_table_change_reaches_workers_the_table_set(db, owner, writer, admin_client):
    wid = _reg(admin_client, 0.9)
    _reg(admin_client, 0.85)  # config/trust.yaml edited: wikidata 0.9 -> 0.85
    assert _worker(owner, wid)["trust"] == pytest.approx(0.85)


def test_without_admin_token_one_hint_and_registration_goes_on(db, owner, writer, plain_client, caplog):
    caplog.set_level(logging.INFO, logger="rebox_worker.client")
    wid = _reg(plain_client, 0.9)
    assert _reg(plain_client, 0.9) == wid
    w = _worker(owner, wid)
    assert w["trust"] == pytest.approx(0.8)  # kind default until someone with ADMIN_TOKEN registers it
    assert w["config"] == {"trust_source": "table", "trust_table": 0.9}
    hints = [r.getMessage() for r in caplog.records if "ADMIN_TOKEN" in r.getMessage()]
    assert hints == [
        f"worker {wid}: trust is 0.8, config/trust.yaml says 0.9; set ADMIN_TOKEN for the worker "
        f"to apply it (or PATCH /v0/admin/workers/{wid})"
    ]
    assert not _patches(writer)


def test_wrong_admin_token_or_unreadable_worker_never_fails_registration(db, owner, writer, caplog):
    c = ClaimsClient(writer.url, writer.token, backoff_s=0.01, max_backoff_s=0.05, admin_token="wrong-token")
    try:
        wid = _reg(c, 0.9)
        assert _worker(owner, wid)["trust"] == pytest.approx(0.8)
        assert "could not set trust 0.9" in caplog.text and "wrong-token" not in caplog.text
        owner.execute("delete from worker where id = %s", (wid,))
    finally:
        c.close()
    # A worker the read API does not list (404): nothing applied, no error.
    real_get = writer.get_worker
    writer.get_worker = lambda _id: None
    try:
        wid = _reg(ClaimsClient(writer.url, writer.token, admin_token=ADMIN), None, name="hn")
        assert _reg(ClaimsClient(writer.url, writer.token, admin_token=ADMIN), 0.7, name="hn") == wid
        assert _worker(owner, wid)["trust"] == pytest.approx(0.8)
    finally:
        writer.get_worker = real_get


def test_register_with_explicit_id(db, owner, writer, plain_client):
    assert _reg(plain_client, None, name="wd", id="wd-connector-000123") == "wd-connector-000123"
    assert _reg(plain_client, None, name="wd", id="wd-connector-000123") == "wd-connector-000123"
    assert owner.execute("select count(*) n from worker").fetchone()["n"] == 1


def test_identity_register_uses_the_table(db, owner, writer, admin_client):
    wid = register(
        admin_client, WorkerSpec(name="dbooth", kind="spider", source="x_spider", layers=("public/x",))
    )
    assert _worker(owner, wid)["trust"] == pytest.approx(0.75)
    pid = register(
        admin_client, WorkerSpec(name="xbio", kind="parser", source="x_parse", layers=("public/x",))
    )
    assert _worker(owner, pid) == {"trust": pytest.approx(0.6), "config": {}}


def test_admin_token_from_env(monkeypatch, writer):
    monkeypatch.setenv("ADMIN_TOKEN", "from-env")
    assert ClaimsClient(writer.url, writer.token).admin_token == "from-env"
    assert ClaimsClient(writer.url, writer.token, admin_token="").admin_token is None
    monkeypatch.delenv("ADMIN_TOKEN")
    assert ClaimsClient(writer.url, writer.token).admin_token is None


# ── against the real API ──


def test_real_api_applies_table_trust_and_respects_admin(real_api, db, owner):  # noqa: F811
    c = ClaimsClient(real_api, "test-ingest-token", backoff_s=0.01, max_backoff_s=0.05, admin_token=ADMIN)
    try:
        wid = _reg(c, 0.9)
        assert _worker(owner, wid) == {
            "trust": pytest.approx(0.9),
            "config": {"trust_source": "table", "trust_table": 0.9},
        }
        old = _reg(c, None, name="hn")
        assert _reg(c, 0.7, name="hn") == old
        assert _worker(owner, old)["trust"] == pytest.approx(0.7)
        r = httpx.patch(
            f"{real_api}/v0/admin/workers/{old}",
            json={"trust": 0.33},
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        assert r.status_code == 200
        # A fresh worker process (a client checks each worker's trust once).
        c2 = ClaimsClient(real_api, "test-ingest-token", admin_token=ADMIN)
        assert _reg(c2, 0.7, name="hn") == old
        c2.close()
        assert _worker(owner, old)["trust"] == pytest.approx(0.33)
    finally:
        c.close()
