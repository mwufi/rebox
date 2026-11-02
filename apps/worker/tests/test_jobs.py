import threading

import pytest

from rebox_worker import jobs
from rebox_worker.db import Db


def test_enqueue_claim_done(db, owner):
    jid = jobs.enqueue(db, "sync", "example")
    job = jobs.claim(db)
    assert job is not None and job.id == jid and job.source == "example" and job.attempts == 1
    assert jobs.claim(db) is None  # running jobs are not claimable
    jobs.mark_done(db, job)
    row = owner.execute("select status, finished_at, locked_at from job where id = %s", (jid,)).fetchone()
    assert row["status"] == "done" and row["finished_at"] is not None and row["locked_at"] is None


def test_enqueue_rejects_unknown_kind(db):
    with pytest.raises(ValueError):
        jobs.enqueue(db, "explode", "example")


def test_claim_concurrency_two_workers_never_share_a_job(db, app_url):
    n = 60
    for i in range(n):
        jobs.enqueue(db, "sync", f"s{i}")
    got: dict[str, list[int]] = {"a": [], "b": []}
    start = threading.Barrier(2)

    def worker(name: str) -> None:
        with Db(app_url, max_size=1) as mine:
            start.wait()
            while (job := jobs.claim(mine)) is not None:
                got[name].append(job.id)
                jobs.mark_done(mine, job)

    threads = [threading.Thread(target=worker, args=(k,)) for k in got]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    a, b = set(got["a"]), set(got["b"])
    assert not a & b
    assert len(a | b) == n and len(got["a"]) + len(got["b"]) == n
    assert got["a"] and got["b"], "both workers should have claimed something"


def test_claim_skips_locked_row(db, app_url, owner):
    jid = jobs.enqueue(db, "sync", "example")
    with Db(app_url, max_size=1) as other, other.view() as conn:
        conn.execute("select id from job where id = %s for update", (jid,))
        assert jobs.claim(db) is None  # SKIP LOCKED: does not block, does not take it
    assert jobs.claim(db).id == jid


def test_retry_backoff_then_failed(db, owner):
    jid = jobs.enqueue(db, "sync", "example", max_attempts=3)
    delays = []
    for attempt in (1, 2):
        job = jobs.claim(db)
        assert job.attempts == attempt
        assert jobs.mark_failed(db, job, f"boom {attempt}", base_s=10) == "queued"
        row = owner.execute(
            "select status, last_error, extract(epoch from run_after - now()) as delay"
            " from job where id = %s",
            (jid,),
        ).fetchone()
        assert row["status"] == "queued" and row["last_error"] == f"boom {attempt}"
        delays.append(float(row["delay"]))
        assert jobs.claim(db) is None  # not ready until the backoff passes
        owner.execute("update job set run_after = now() where id = %s", (jid,))
    assert 8 <= delays[0] <= 10 and 18 <= delays[1] <= 20  # 10s, then 20s
    job = jobs.claim(db)
    assert job.attempts == 3
    assert jobs.mark_failed(db, job, "boom 3") == "failed"
    row = owner.execute("select status, attempts, finished_at from job where id = %s", (jid,)).fetchone()
    assert row["status"] == "failed" and row["attempts"] == 3 and row["finished_at"] is not None


def test_backoff_formula():
    assert [jobs.backoff_s(a, 60, 3600) for a in (1, 2, 3, 4, 10)] == [60, 120, 240, 480, 3600]


def test_mark_failed_without_retry(db):
    jobs.enqueue(db, "sync", "nope", max_attempts=5)
    assert jobs.mark_failed(db, jobs.claim(db), "unknown connector", retry=False) == "failed"


def test_requeue_stale(db, owner):
    jid = jobs.enqueue(db, "sync", "example")
    jobs.claim(db)
    owner.execute("update job set locked_at = now() - interval '7 hours' where id = %s", (jid,))
    assert jobs.requeue_stale(db) == 1
    assert owner.execute("select status from job where id = %s", (jid,)).fetchone()["status"] == "queued"


def test_schedule_due_idempotent(db, owner):
    owner.execute(
        """insert into connector_state (source, interval_s, next_run_at) values
             ('due', 3600, now() - interval '1 minute'),
             ('later', 3600, now() + interval '1 hour'),
             ('busy', 60, now() - interval '1 minute')"""
    )
    owner.execute(
        """insert into job (kind, payload, status) values ('sync', '{"source": "busy"}', 'running')"""
    )

    assert jobs.schedule_due(db) == ["due"]
    assert jobs.schedule_due(db) == []  # second run: nothing is due any more
    queued = owner.execute(
        "select payload->>'source' s, count(*) n from job where status = 'queued' group by 1"
    ).fetchall()
    assert [(r["s"], r["n"]) for r in queued] == [("due", 1)]
    states = {
        r["source"]: r
        for r in owner.execute(
            "select source, extract(epoch from next_run_at - now()) as d from connector_state"
        ).fetchall()
    }
    assert 3590 <= float(states["due"]["d"]) <= 3600
    assert 50 <= float(states["busy"]["d"]) <= 60  # advanced, but no second job enqueued
    assert 3500 <= float(states["later"]["d"]) <= 3600  # untouched


def test_schedule_due_does_not_double_enqueue_while_queued(db, owner):
    owner.execute("insert into connector_state (source, interval_s, next_run_at) values ('x', 60, now())")
    assert jobs.schedule_due(db) == ["x"]
    owner.execute("update connector_state set next_run_at = now() - interval '1 second'")
    assert jobs.schedule_due(db) == []  # the first job is still queued
    assert owner.execute("select count(*) n from job").fetchone()["n"] == 1


class _Disabled:
    name, layer, interval_s = "off", "public/off", 60

    def enabled(self, ctx):
        return False


class _On:
    name, layer, interval_s = "on", "public/on", 60


def test_schedule_due_registers_connectors_and_skips_disabled(db, owner):
    assert jobs.schedule_due(db, {"off": _Disabled(), "on": _On()}) == ["on"]
    rows = owner.execute("select source, interval_s from connector_state order by source").fetchall()
    assert [(r["source"], r["interval_s"]) for r in rows] == [("off", 60), ("on", 60)]
