"""End to end: example connector -> raw store -> resolver -> fake writer -> Postgres (app role)."""

import json
import shutil

import pytest

from rebox_worker import cli, jobs
from rebox_worker.client import ValidationRejected
from rebox_worker.connectors import get_connector
from rebox_worker.pipeline import SourceBusy, _source_lock, run_replay, run_sync

# What the five fixture files must produce (see connectors/example.py).
EXPECTED_ENTITIES = {"company": 2, "person": 3}
EXPECTED_CLAIMS = 35


def counts(owner):
    kinds = {r["kind"]: r["n"] for r in owner.execute("select kind, count(*) n from entity group by kind")}
    claims = owner.execute("select count(*) n from claim").fetchone()["n"]
    return kinds, claims


def entity_by(owner, predicate, value):
    rows = owner.execute(
        "select distinct subject from claim where predicate = %s and value #>> '{}' = %s", (predicate, value)
    ).fetchall()
    assert len(rows) == 1, rows
    return rows[0]["subject"]


def test_sync_example_end_to_end(deps, owner):
    stats = run_sync(get_connector("example"), deps)

    assert counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)
    assert stats.as_dict() == {
        "fetched": 5,
        "skipped_unchanged": 0,
        "extract_errors": 0,
        "claims": EXPECTED_CLAIMS,
        "written": EXPECTED_CLAIMS,
        "minted": 5,
        "matched": 6,
        "review": 0,
        "rejected": 0,
        "batches": 5,
    }
    # Every entity has exactly one name, and lives in the connector's layer.
    names = owner.execute(
        "select e.id, count(c.id) n from entity e"
        " left join claim c on c.subject = e.id and c.predicate = 'name' group by e.id"
    ).fetchall()
    assert all(r["n"] >= 1 for r in names) and len(names) == 5
    assert {r["home_layer"] for r in owner.execute("select home_layer from entity")} == {"public/example"}

    # Hard-key match across files: Ada (github written two ways) is one person with both handles.
    ada = entity_by(owner, "github", "ada-example")
    assert entity_by(owner, "x_handle", "ada_example") == ada
    # Acme matched by domain (03, 04) and by github (05).
    acme = entity_by(owner, "domain", "acme-robotics.example")
    assert entity_by(owner, "github", "acme-robotics") == acme
    edges = owner.execute(
        "select subject, predicate, object_id, value from claim"
        " where object_id is not null order by predicate, subject"
    ).fetchall()
    assert {(e["predicate"], e["object_id"]) for e in edges} >= {("worked_at", acme), ("founded", acme)}

    # Scoped fuzzy: Bo (no identity key) in 04 matched the Bo minted in 01 via Acme.
    bo_names = owner.execute(
        "select distinct subject from claim where predicate = 'name' and value #>> '{}' like 'Bo%%'"
    )
    assert len(bo_names.fetchall()) == 1
    fuzzy = owner.execute(
        "select entity_id, confidence from resolution_log where evidence->0->>'pass' = 'scoped_fuzzy'"
    ).fetchall()
    assert len(fuzzy) == 1 and fuzzy[0]["confidence"] == pytest.approx(0.8)

    decisions = {
        r["decision"]: r["n"]
        for r in owner.execute("select decision, count(*) n from resolution_log group by 1")
    }
    assert decisions == {"minted": 5, "matched": 6}
    assert owner.execute("select count(*) n from review_item").fetchone()["n"] == 0

    state = owner.execute(
        "select cursor, last_status, last_stats, interval_s from connector_state"
    ).fetchone()
    assert state["cursor"] == {"last_file": "05-cy.json"} and state["last_status"] == "ok"
    assert state["last_stats"]["minted"] == 5 and state["interval_s"] == 86400
    assert owner.execute("select count(*) n from raw_record where source = 'example'").fetchone()["n"] == 5
    # The change feed saw every claim insert.
    assert owner.execute("select count(*) n from change").fetchone()["n"] == EXPECTED_CLAIMS


def test_resync_skips_unchanged(deps, owner):
    run_sync(get_connector("example"), deps)
    stats = run_sync(get_connector("example"), deps)
    assert (stats.fetched, stats.skipped_unchanged, stats.claims, stats.minted) == (5, 5, 0, 0)
    assert counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)


def test_replay_creates_no_new_entities(deps, owner):
    run_sync(get_connector("example"), deps)
    before = owner.execute("select id from entity order by id").fetchall()
    for _ in range(2):
        stats = run_replay(get_connector("example"), deps)
        assert stats.fetched == 5 and stats.minted == 0 and stats.matched > 0
        assert owner.execute("select id from entity order by id").fetchall() == before
        assert counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)  # upserts, no duplicates


def test_replay_from_scratch_db_is_deterministic(deps, owner, owner_url):
    """Wipe the graph, replay from the raw store: the same ids come back."""
    run_sync(get_connector("example"), deps)
    before = [r["id"] for r in owner.execute("select id from entity order by id")]
    owner.execute("truncate claim, entity, change, resolution_log cascade")
    stats = run_replay(get_connector("example"), deps)
    assert stats.minted == 5
    assert [r["id"] for r in owner.execute("select id from entity order by id")] == before


def test_single_batch_resolves_like_many(deps, owner, monkeypatch):
    """With every record in one batch, the batch cache must give the same graph."""
    run_sync(get_connector("example"), deps)
    before = counts(owner), [r["id"] for r in owner.execute("select id from entity order by id")]
    owner.execute("truncate claim, entity, change, resolution_log cascade")
    stats = run_replay(get_connector("example"), deps, batch_records=100)
    assert stats.batches == 1 and stats.minted == 5
    assert (counts(owner), [r["id"] for r in owner.execute("select id from entity order by id")]) == before


def test_failed_write_does_not_advance_cursor_or_index(deps, owner, writer):
    writer.fail_next = [400]
    deps.client.max_retries = 0

    with pytest.raises(Exception):  # noqa: B017 - the injected 400 has no details payload
        run_sync(get_connector("example"), deps)
    state = owner.execute("select cursor, last_status from connector_state").fetchone()
    assert state["cursor"] is None and state["last_status"].startswith("error:")
    assert owner.execute("select count(*) n from raw_record").fetchone()["n"] == 0
    # The retry then processes everything (nothing was wrongly skipped as "unchanged").
    stats = run_sync(get_connector("example"), deps)
    assert stats.skipped_unchanged == 0 and counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)


def test_writer_validation_error_surfaces(deps, writer, monkeypatch):
    monkeypatch.setattr(writer, "_validate", lambda *a: "nope")
    with pytest.raises(ValidationRejected, match="nope"):
        run_sync(get_connector("example"), deps)


def test_record_for_another_source_is_refused(deps, owner):
    from rebox_worker.models import RawRecord

    class Wrong:
        name, layer, interval_s = "wrong", "public/wrong", 60

        def fetch(self, ctx, cursor):
            yield RawRecord.new("example", "x", {})

        def extract(self, rec):
            return []

    with pytest.raises(ValueError, match="for source 'example'"):
        run_sync(Wrong(), deps)
    assert owner.execute("select count(*) n from raw_record").fetchone()["n"] == 0


def test_source_lock_prevents_concurrent_runs(deps):
    with _source_lock(deps, "example"), pytest.raises(SourceBusy):
        run_sync(get_connector("example"), deps)


def test_fixtures_dir_override(deps, tmp_path, monkeypatch, owner):
    src = deps.ctx.fixtures / "example"
    alt = tmp_path / "fx" / "raw" / "example"
    alt.mkdir(parents=True)
    shutil.copy(src / "01-acme.json", alt)
    from rebox_worker.connectors.base import fixtures_root

    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path / "fx"))
    assert fixtures_root() == tmp_path / "fx" / "raw"
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path / "fx" / "raw"))
    assert fixtures_root() == tmp_path / "fx" / "raw"
    deps.ctx.fixtures = fixtures_root()
    assert run_sync(get_connector("example"), deps).fetched == 1


# ── CLI ──


@pytest.fixture
def env(monkeypatch, app_url, writer, tmp_path, db):
    monkeypatch.setenv("DATABASE_URL", app_url)
    monkeypatch.setenv("API_URL", writer.url)
    monkeypatch.setenv("INGEST_TOKEN", writer.token)
    monkeypatch.setenv("RAW_STORE", f"file://{tmp_path}/raw")
    monkeypatch.setenv("X_BEARER_TOKEN", "unused")  # unknown/optional env vars are fine
    monkeypatch.delenv("FIXTURES_DIR", raising=False)


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0 and "schedule-due" in capsys.readouterr().out


def test_cli_sync_status_replay(env, owner, capsys):
    assert cli.main(["sync", "example"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("sync example: ")
    assert json.loads(out.split(": ", 1)[1])["minted"] == 5
    assert cli.main(["status"]) == 0
    status = capsys.readouterr().out
    assert "example" in status and "last_stats" in status
    assert cli.main(["replay", "example"]) == 0
    assert json.loads(capsys.readouterr().out.split(": ", 1)[1])["minted"] == 0
    assert counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)


def test_cli_enqueue_run_once(env, owner, capsys):
    assert cli.main(["enqueue", "example"]) == 0
    assert cli.main(["enqueue", "example", "--kind", "replay"]) == 0
    assert cli.main(["run", "--once"]) == 0
    rows = owner.execute("select kind, status, attempts from job order by id").fetchall()
    assert [(r["kind"], r["status"], r["attempts"]) for r in rows] == [
        ("sync", "done", 1),
        ("replay", "done", 1),
    ]
    assert counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)


def test_cli_run_marks_unknown_source_failed(env, owner, db):
    jobs.enqueue(db, "sync", "no-such-source")
    assert cli.main(["run", "--once"]) == 0
    row = owner.execute("select status, last_error from job").fetchone()
    assert row["status"] == "failed" and "unknown connector" in row["last_error"]


def test_job_without_source_fails_instead_of_sticking(deps, owner):
    owner.execute("insert into job (kind, payload) values ('sync', '{}')")
    assert cli.work_once(deps)
    row = owner.execute("select status, attempts, last_error from job").fetchone()
    assert row["status"] == "failed" and row["attempts"] == 1 and "unknown connector" in row["last_error"]


def test_cli_schedule_due(env, owner, capsys, monkeypatch):
    # github and x are disabled without their tokens (S4); hn and wikidata need none; example is
    # test data and never scheduled.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("X_BEARER_TOKEN", "")  # compose passes empty values for unset tokens
    assert cli.main(["schedule-due"]) == 0
    assert capsys.readouterr().out.strip() == "schedule-due: enqueued hn, wikidata"
    assert cli.main(["schedule-due"]) == 0
    assert capsys.readouterr().out.strip() == "schedule-due: enqueued nothing"
    # A token in the environment (Ctx(env=os.environ)) enables the connector on the next run.
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert cli.main(["schedule-due"]) == 0
    assert capsys.readouterr().out.strip() == "schedule-due: enqueued github"


def test_cli_unknown_source_and_missing_env(env, monkeypatch, capsys):
    assert cli.main(["sync", "nope"]) == 2
    assert "unknown connector" in capsys.readouterr().err
    monkeypatch.setenv("API_URL", "localhost:3000")  # no scheme: fail fast, do not retry
    assert cli.main(["sync", "example"]) == 2
    assert "http(s) URL" in capsys.readouterr().err
    monkeypatch.delenv("API_URL")
    assert cli.main(["sync", "example"]) == 2
    assert "API_URL" in capsys.readouterr().err


def test_job_runner_requeues_with_backoff_on_writer_outage(deps, owner, writer):
    deps.client.max_retries = 1
    writer.fail_next = [503] * 2  # one job attempt = 1 try + 1 client retry
    jid = jobs.enqueue(deps.db, "sync", "example", max_attempts=2)
    assert cli.work_once(deps)
    row = owner.execute(
        "select status, attempts, last_error, run_after > now() as later from job where id = %s", (jid,)
    ).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1 and row["later"]
    assert "503" in row["last_error"]
    owner.execute("update job set run_after = now()")
    assert cli.work_once(deps)  # writer is healthy again
    assert owner.execute("select status from job").fetchone()["status"] == "done"
    assert counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)
