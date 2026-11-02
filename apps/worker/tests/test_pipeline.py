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


def test_writer_rejecting_every_claim_does_not_stop_the_run(deps, owner, writer, monkeypatch):
    """Per-claim 400s drop those claims; a batch where everything is refused also continues."""
    monkeypatch.setattr(writer, "_validate", lambda *a: "nope")
    stats = run_sync(get_connector("example"), deps)
    # (Nothing is written, so later records cannot match earlier ones: more claims than usual.)
    assert stats.written == stats.minted == 0 and stats.rejected == stats.claims > 0 and stats.batches == 5
    assert counts(owner) == ({}, 0)
    run = owner.execute("select status, stats from run").fetchone()
    assert run["status"] == "ok" and run["stats"]["rejected"] == stats.claims
    state = owner.execute("select cursor, last_status from connector_state").fetchone()
    assert state["last_status"] == "ok" and state["cursor"] is not None
    # The records are indexed (a replay after a fix re-extracts them; a sync skips them).
    assert owner.execute("select count(*) n from raw_record").fetchone()["n"] == 5
    monkeypatch.undo()
    stats = run_replay(get_connector("example"), deps)
    assert stats.rejected == 0 and counts(owner) == (EXPECTED_ENTITIES, EXPECTED_CLAIMS)


def test_batch_level_400_still_fails_the_run(deps, owner, writer):
    """A 400 not about specific claims (worker not active, layer not allowed) cannot be skipped."""
    run_sync(get_connector("example"), deps)  # registers the worker
    owner.execute("update worker set status = 'paused'")
    owner.execute("update connector_state set cursor = null")
    owner.execute("truncate raw_record")
    with pytest.raises(ValidationRejected, match="may not write"):
        run_sync(get_connector("example"), deps)
    run = owner.execute("select status, error from run order by id desc limit 1").fetchone()
    assert run["status"] == "failed" and "may not write" in run["error"]


class _Incident:
    """The X spider incident (S12): one page of 500 accounts, account #250's website is
    ``http://www.com``, which the old API normalized differently from the worker."""

    name, layer, interval_s = "incident", "public/incident", 60

    def fetch(self, ctx, cursor):
        from rebox_worker.models import CursorUpdate, RawRecord

        yield RawRecord.new(self.name, "page-1", {"n": 500})
        yield CursorUpdate(cursor={"page": 1})

    def extract(self, rec):
        from rebox_worker.models import ClaimInput, EntityRef

        return [
            ClaimInput(
                subject=EntityRef(kind="company", identity={"x_handle": f"acct{i}"}),
                predicate="website",
                value="http://www.com" if i == 250 else f"https://c{i}.example",
                source_ref=f"incident:{i}:website",
            )
            for i in range(rec.payload["n"])
        ]


def _old_api_url(value):
    """The API's url rule before S12: drop a leading ``www.`` unconditionally."""
    import re

    m = re.match(r"^([a-z][a-z0-9+.-]*)://([^/?#]+)([^?#]*)", value.strip(), re.I)
    host = m.group(2).lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{m.group(1).lower()}://{host}{m.group(3).rstrip('/')}"


def test_incident_one_non_normalized_identity_among_500(deps, owner, writer, monkeypatch, caplog):
    import logging

    import fake_writer

    caplog.set_level(logging.WARNING, logger="rebox_worker.client")

    real = fake_writer._norm
    monkeypatch.setattr(
        fake_writer, "_norm", lambda meta: _old_api_url if meta.get("normalize") == "url" else real(meta)
    )
    stats = run_sync(_Incident(), deps)

    assert len(writer.requests) == 2 and len(writer.requests[0]["claims"]) == 500  # one batch, re-sent
    assert (stats.claims, stats.written, stats.rejected, stats.minted) == (500, 499, 1, 499)
    assert owner.execute("select count(*) n from claim").fetchone()["n"] == 499
    assert owner.execute("select count(*) n from entity").fetchone()["n"] == 499
    assert not owner.execute("select 1 from claim where value #>> '{}' = 'http://www.com'").fetchall()
    run = owner.execute("select status, stats from run").fetchone()
    assert run["status"] == "ok" and (run["stats"]["written"], run["stats"]["rejected"]) == (499, 1)
    state = owner.execute("select cursor, last_status from connector_state").fetchone()
    assert state == {"cursor": {"page": 1}, "last_status": "ok"}
    logged = [r.message for r in caplog.records if "writer rejected claim" in r.message]
    assert len(logged) == 1 and "incident:250:website" in logged[0] and "'http://www.com'" in logged[0]
    assert "identity value for website not normalized" in logged[0]

    # With the S12 normalizers both sides agree on http://www.com: a replay writes all 500.
    monkeypatch.setattr(fake_writer, "_norm", real)
    stats = run_replay(_Incident(), deps)
    assert (stats.written, stats.rejected) == (500, 0)
    assert owner.execute("select count(*) n from claim").fetchone()["n"] == 500


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
