# ruff: noqa: E501  (inline synthetic payloads read better on one line)
"""S19: ``replay <source> --retract-stale`` retracts what the current extract no longer emits.

Stored ``exa_parse`` outputs replay with no model. An "old" extract (a subclass that also emits a
milestone the current one drops, like the S18 relevance filter did) writes the claims that later
go stale.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from test_provenance import real_api  # noqa: F401 - module fixture: the real API on the test DB

from rebox_worker import cli
from rebox_worker.client import ClaimsClient
from rebox_worker.connectors.exa_parse import ExaParse
from rebox_worker.models import ClaimInput, ClaimWrite, EntityRef, IdRef, RawRecord
from rebox_worker.parsers.prompt import PROMPT_VERSION
from rebox_worker.pipeline import Deps, RetractStale, replay_retract_stale, run_replay

T0 = datetime(2026, 10, 25, 12, tzinfo=UTC)
LAYER = "public/exa_ai"


def stored(i: int) -> RawRecord:
    name = f"Acme{i}"
    ms = {"type": "milestone", "kind": "funding", "title": f"{name} raised $5M", "org": name, "org_handle": "",
          "first_person": False, "quote": f"{name} raised $5M"}  # fmt: skip
    payload = {"input_ref": f"exa:a{i}", "input_hash": "", "model": "claude-haiku-5-5", "prompt_version": PROMPT_VERSION,
               "input": {"kind": "article", "fields": {"title": f"{name} raises $5M", "text": f"{name} raised $5M from Lux."},
                         "meta": {"date": "2026-09-05", "url": f"https://news.example/a{i}", "author": "news.example",
                                  "host": "news.example"}},
               "output": {"account": "other", "facts": [ms]}, "graph": {}}  # fmt: skip
    return RawRecord.new("exa_parse", f"exa:a{i}", payload, fetched_at=T0)


class NoModel:
    name = "claude-haiku-5-5"

    def parse(self, docs, usage):
        raise AssertionError("replay must not call the model")


class OldExa(ExaParse):
    """What an older extract emitted: also an 'old' milestone for the records in ``stale``."""

    def __init__(self, stale: set[int]):
        super().__init__(model=NoModel())
        self.stale = stale

    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        out = super().extract(rec)
        i = int(rec.external_id.removeprefix("exa:a"))
        if i in self.stale:
            url = f"https://news.example/a{i}"
            out.append(
                ClaimInput(
                    subject=EntityRef(kind="company", name=f"Acme{i}"),
                    predicate="milestone",
                    value={"title": "old listicle milestone", "kind": "other", "url": url},
                    valid_from="2026-09-05",
                    source_ref=f"exa_parse:exa:a{i}:milestone:old",
                    evidence=[{"url": url, "quote": f"Acme{i} raised $5M"}],
                )
            )
        return out


def seed(deps, n: int, stale: set[int]) -> str:
    for i in range(n):
        deps.raw.put(stored(i))
    run_replay(OldExa(stale), deps)
    return deps.client.register_worker(name="exa", kind="parser", source="exa_parse", layers=[LAYER])


def live(owner, where: str = "true") -> list[dict[str, Any]]:
    return owner.execute(
        f"select source_ref, worker_id, retract, run_id from claim where layer_id = %s and {where} order by source_ref",
        (LAYER,),
    ).fetchall()


def counts(owner) -> tuple:
    return tuple(
        owner.execute(
            "select (select count(*) from claim) c, (select count(*) from change) ch, (select count(*) from run) r,"
            " (select count(*) from claim where retract) x"
        )
        .fetchone()
        .values()
    )


def _write(deps, worker: str, claims: list[ClaimWrite], run_stats: dict | None = None) -> None:
    run = deps.client.open_run(worker, "replay")
    deps.client.write(LAYER, claims, worker=worker, run_id=run)
    deps.client.update_run(run, status="ok", stats=run_stats or {"x": 1})


def _ms(subject: str, ref: str) -> ClaimWrite:
    return ClaimWrite(
        subject=IdRef(id=subject),
        predicate="milestone",
        value={"title": "t", "kind": "other", "url": "https://n.example/1"},
        valid_from="2026-09-05",
        source_ref=ref,
        evidence=[{"url": "https://n.example/1", "quote": "q"}],
    )


def test_retract_stale_dry_run_then_retract_idempotent(deps, owner):
    exa = seed(deps, 10, {3, 7})
    company = owner.execute("select subject from claim where predicate = 'milestone' limit 1").fetchone()[
        "subject"
    ]
    # Claims that are never stale: another worker in the same layer (x_parse_posts shares a
    # layer with x_parse), a claim an import wrote, and a resolver-added name claim.
    other = deps.client.register_worker(name="other", kind="parser", source="other_parse", layers=[LAYER])
    _write(deps, other, [_ms(company, "other:not-emitted")])
    imp = deps.client.register_worker(name="imp", kind="parser", source="exa_parse", layers=[LAYER])
    _write(
        deps,
        imp,
        [_ms(company, "exa_parse:remote:milestone:x")],
        {"import": {"source_instance": "overnight"}},
    )
    name = ClaimWrite(subject=IdRef(id=company), predicate="name", value="Acme", source_ref=f"exa_parse:entity:{company}:name",
                      evidence=[{"url": "https://n.example/1", "quote": "Acme"}])  # fmt: skip
    _write(deps, exa, [name])
    before = counts(owner)

    dry = replay_retract_stale(ExaParse(model=NoModel()), deps, RetractStale(dry_run=True))
    assert counts(owner) == before  # nothing written, no runs
    assert dry.records == 10 and dry.extract_errors == 0 and dry.refused is None
    assert (
        dry.stale == {LAYER: 2}
        and dict(dry.by_predicate) == {"milestone": 2}
        and dict(dry.by_worker) == {exa: 2}
    )
    assert dry.considered == {
        LAYER: 22
    }  # 10 milestones + 10 names + 2 old (not: other, import, resolver name)
    assert sorted(e["source_ref"] for e in dry.examples) == [
        "exa_parse:exa:a3:milestone:old",
        "exa_parse:exa:a7:milestone:old",
    ]
    assert dry.retracted == 0 and "DRY RUN" in dry.render() and "milestone 2" in dry.render()

    report = replay_retract_stale(ExaParse(model=NoModel()), deps)
    assert report.refused is None and report.retracted == 2 and report.rejected == 0
    gone = live(owner, "retract")
    assert [(r["source_ref"], r["worker_id"]) for r in gone] == [
        ("exa_parse:exa:a3:milestone:old", exa),
        ("exa_parse:exa:a7:milestone:old", exa),
    ]
    # In the replay's own run of that worker (kind replay), which counts them.
    run = owner.execute(
        "select id, kind, status, stats from run where id = %s", (gone[0]["run_id"],)
    ).fetchone()
    assert run["id"] == report.runs[exa] and run["kind"] == "replay" and run["status"] == "ok"
    assert run["stats"]["retracted_stale"] == 2 and run["stats"]["fetched"] == 10
    # Untouched: the other worker, the import, the resolver's name claim, every emitted claim.
    assert len(live(owner, "not retract")) == 23
    assert {r["source_ref"] for r in live(owner, f"not retract and worker_id <> '{exa}'")} == {
        "other:not-emitted",
        "exa_parse:remote:milestone:x",
    }

    after = counts(owner)
    again = replay_retract_stale(ExaParse(model=NoModel()), deps)
    assert again.total_stale == 0 and again.retracted == 0 and again.refused is None
    c, _, _, x = counts(owner)
    assert (c, x) == (after[0], after[3])  # no new claims, nothing more retracted


def test_guard_refuses_more_than_30_percent_unless_forced(deps, owner):
    exa = seed(deps, 3, {0, 1, 2})  # 3 stale of 9 claims: 33%
    report = replay_retract_stale(ExaParse(model=NoModel()), deps)
    assert report.refused and "3 of 9 claims (33%)" in report.refused and "--force" in report.refused
    assert report.retracted == 0 and live(owner, "retract") == []
    assert "REFUSED" in report.render()
    dry = replay_retract_stale(ExaParse(model=NoModel()), deps, RetractStale(dry_run=True))
    assert dry.refused and dry.stale == {LAYER: 3}
    forced = replay_retract_stale(ExaParse(model=NoModel()), deps, RetractStale(force=True))
    assert forced.refused is None and forced.retracted == 3
    assert {r["worker_id"] for r in live(owner, "retract")} == {exa}


def test_extract_errors_refuse(deps, owner):
    seed(deps, 10, {1})

    class Broken(ExaParse):
        def extract(self, rec):
            if rec.external_id == "exa:a5":
                raise ValueError("boom")
            return super().extract(rec)

    report = replay_retract_stale(Broken(model=NoModel()), deps)
    assert report.extract_errors == 1 and report.refused and "failed to extract" in report.refused
    assert live(owner, "retract") == []


def test_cli_retract_stale(deps, owner, writer, app_url, tmp_path, monkeypatch, capsys):
    seed(deps, 10, {4})
    monkeypatch.setenv("DATABASE_URL", app_url)
    monkeypatch.setenv("RAW_STORE", f"file://{tmp_path / 'raw'}")
    monkeypatch.setenv("API_URL", writer.url)
    monkeypatch.setenv("INGEST_TOKEN", writer.token)
    assert cli.main(["replay", "exa_parse", "--dry-run"]) == 2  # --dry-run needs --retract-stale
    assert cli.main(["replay", "exa_parse", "--retract-stale", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and f"{LAYER}: 21 claims of these workers, stale 1 (4.8%)" in out
    assert "exa_parse:exa:a4:milestone:old" in out and live(owner, "retract") == []
    assert cli.main(["replay", "exa_parse", "--retract-stale"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("replay exa_parse: {") and "retracted 1, rejected 0" in out
    assert [r["source_ref"] for r in live(owner, "retract")] == ["exa_parse:exa:a4:milestone:old"]


def test_no_workers_no_records_is_a_no_op(deps, owner):
    report = replay_retract_stale(ExaParse(model=NoModel()), deps, RetractStale(dry_run=True))
    assert report.workers == [] and report.total_stale == 0 and report.refused is None


def test_against_the_real_writer(real_api, db, owner, tmp_path):  # noqa: F811
    """The retraction goes through the real writer: same upsert key (no new claim), evidence and
    dates accepted, and a re-run changes nothing (no claim or change rows)."""
    from rebox_worker.rawstore import FileRawStore

    client = ClaimsClient(real_api, "test-ingest-token", backoff_s=0.01, max_backoff_s=0.05, admin_token="")
    try:
        deps = Deps(db=db, raw=FileRawStore(tmp_path / "raw", db), client=client)
        exa = seed(deps, 10, {2})
        n = owner.execute("select count(*) n from claim").fetchone()["n"]
        report = replay_retract_stale(ExaParse(model=NoModel()), deps)
        assert report.retracted == 1 and report.rejected == 0
        (row,) = owner.execute("select worker_id, evidence, valid_from from claim where retract").fetchall()
        assert row["worker_id"] == exa and row["evidence"][0]["quote"] == "Acme2 raised $5M"
        assert owner.execute("select count(*) n from claim").fetchone()["n"] == n
        before = counts(owner)
        replay_retract_stale(ExaParse(model=NoModel()), deps)
        c, ch, _, x = counts(owner)
        assert (c, ch, x) == (before[0], before[1], before[3])
    finally:
        client.close()


def test_last_name_of_an_entity_with_facts_is_kept(deps, owner):
    """A stale name that is an entity's only name while the entity still has live facts (an
    on-topic article references it by id) is kept; a stale name of an otherwise empty entity goes."""
    from rebox_worker.models import MintRef, MintSpec

    exa = seed(deps, 10, set())
    ev = [{"url": "https://n.example/l", "quote": "Keptco and Orphanco"}]
    names = [
        ClaimWrite(
            subject=MintRef(mint=MintSpec(id=f"rbx:company:{n.lower()}", kind="company")),
            predicate="name",
            value=n,
            source_ref=f"exa_parse:exa:listicle:name:name={n.lower()}",
            evidence=ev,
        )
        for n in ("Keptco", "Orphanco")
    ]
    _write(deps, exa, names)
    _write(deps, exa, [_ms("rbx:company:keptco", "exa_parse:exa:a1:milestone:funding:rbx:company:keptco")])
    # That milestone is not emitted either, so make it current: another worker's live fact on Keptco.
    other = deps.client.register_worker(name="other", kind="parser", source="other_parse", layers=[LAYER])
    _write(deps, other, [_ms("rbx:company:keptco", "other:keptco")])
    dry = replay_retract_stale(ExaParse(model=NoModel()), deps, RetractStale(dry_run=True))
    assert dry.kept_names == 1 and dict(dry.by_predicate) == {"name": 1, "milestone": 1}
    assert "kept 1 stale name claims" in dry.render()
    report = replay_retract_stale(ExaParse(model=NoModel()), deps)
    assert report.retracted == 2
    rows = owner.execute(
        "select subject, predicate, retract from claim where subject in ('rbx:company:keptco', 'rbx:company:orphanco')"
        " and worker_id = %s order by 1, 2",
        (exa,),
    ).fetchall()
    assert [tuple(r.values()) for r in rows] == [
        ("rbx:company:keptco", "milestone", True),
        ("rbx:company:keptco", "name", False),
        ("rbx:company:orphanco", "name", True),
    ]
