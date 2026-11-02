# ruff: noqa: E501  (inline synthetic payloads read better on one line)
"""Parser connectors through the real pipeline: upstream raw records -> fetch (recorded model) ->
raw store -> extract -> resolver -> fake writer -> Postgres. Replay makes zero model calls.

The fake writer enforces CONTRACTS §12 (workers, runs, evidence required for parser workers).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import RecordedModel, load_recorded

from rebox_worker.connectors import REGISTRY, get_connector, is_enabled
from rebox_worker.connectors.base import Ctx
from rebox_worker.connectors.exa_parse import ExaParse
from rebox_worker.connectors.github_parse import GitHubParse
from rebox_worker.connectors.x_parse import XParse, XParsePosts
from rebox_worker.models import ClaimInput, CursorUpdate, RawRecord
from rebox_worker.parsers.prompt import PROMPT_VERSION
from rebox_worker.pipeline import run_replay, run_sync

T0 = datetime(2026, 10, 25, 12, tzinfo=UTC)


def seed_upstream(
    deps, source: str, payloads: list[tuple[str, dict[str, Any]]], start: datetime = T0
) -> None:
    for i, (ext, payload) in enumerate(payloads):
        rec = RawRecord.new(source, ext, payload, fetched_at=start + timedelta(seconds=i))
        deps.raw.put(rec)


def fixture_inputs(path: str) -> list[tuple[str, str, dict[str, Any]]]:
    data = load_recorded(path)
    prefix = path.split("/")[-1].removesuffix(".json")
    return [(it["source"], f"{prefix}:{i}", it["payload"]) for i, it in enumerate(data["inputs"])]


def recorded_model(*paths: str) -> RecordedModel:
    outputs: dict[str, Any] = {}
    for p in paths:
        outputs.update(load_recorded(p)["outputs"])
    return RecordedModel(outputs)


def with_env(deps, **env: str):
    deps.ctx = Ctx(fixtures=deps.ctx.fixtures, env={"PARSER_BATCH": "10", **env}, limit=deps.ctx.limit)
    return deps


# ── registry and enablement ──


def test_registered_with_layers_and_worker_names():
    expected = {
        "x_parse": ("public/x_ai", "xbio"),
        "x_parse_posts": ("public/x_ai", "xpost"),
        "github_parse": ("public/github_ai", "github"),
        "exa_parse": ("public/exa_ai", "exa"),
    }
    for name, (layer, worker) in expected.items():
        c = get_connector(name)
        assert REGISTRY[name] is type(c) and c.layer == layer
        assert (c.worker_name, c.worker_kind) == (worker, "parser")


def test_disabled_without_key():
    c = XParse()
    assert not is_enabled(c, Ctx(env={}))
    assert is_enabled(c, Ctx(env={"ANTHROPIC_API_KEY": "k"}))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        next(iter(c.fetch(Ctx(env={}, db=object()), None)))


# ── fetch ──


def test_fetch_batches_and_stores_model_output(deps, owner):
    seed_upstream(
        deps, "x_spider", [(ext, p) for s, ext, p in fixture_inputs("x_parse/bios.json") if s == "x_spider"]
    )
    model = recorded_model("x_parse/bios.json")
    conn = XParse(model=model, raw=deps.raw)
    stats = run_sync(conn, with_env(deps, PARSER_BATCH="4"))

    assert [len(c) for c in model.calls] == [4, 2]  # 6 bios, batches of 4
    assert stats.fetched == 6 and stats.extract_errors == 0 and stats.rejected == 0
    rec = deps.raw.get(
        owner.execute(
            "select store_key from raw_record where source = 'x_parse' order by external_id limit 1"
        ).fetchone()["store_key"]
    )
    assert set(rec.payload) == {
        "input_ref",
        "input_hash",
        "model",
        "prompt_version",
        "input",
        "output",
        "graph",
    }
    assert rec.payload["prompt_version"] == PROMPT_VERSION and rec.payload["model"] == "claude-haiku-5-5"
    assert rec.external_id == rec.payload["input_ref"] == "x_spider:bios:0"
    cursor = owner.execute("select cursor from connector_state where source = 'x_parse'").fetchone()["cursor"]
    assert cursor["prompt_version"] == PROMPT_VERSION and cursor["after"][2] == "bios:5"
    assert cursor["last_run"]["api_calls"] == 2


def test_claims_land_with_evidence(deps, owner, writer):
    seed_upstream(
        deps, "x_spider", [(ext, p) for s, ext, p in fixture_inputs("x_parse/bios.json") if s == "x_spider"]
    )
    run_sync(XParse(model=recorded_model("x_parse/bios.json"), raw=deps.raw), with_env(deps))

    preds = {r["predicate"]: r["n"] for r in owner.execute(
        "select predicate, count(*) n from claim where layer_id = 'public/x_ai' group by 1")}  # fmt: skip
    assert {"worked_at", "founded", "based_in", "name", "x_handle"} <= set(preds)
    # Every claim the parser sent carried evidence (CONTRACTS §12: required for parser workers).
    sent = [c for body in writer.requests for c in body["claims"]]
    with_ev = {c["source_ref"] for c in sent if c.get("evidence")}
    parser_refs = {c["source_ref"] for c in sent if c["source_ref"].startswith("x_parse:")}
    assert parser_refs and parser_refs <= with_ev
    # Written as a parser worker xbio-parser-NNNNNN, in a run whose stats carry the model usage.
    workers = {body["worker"] for body in writer.requests}
    assert len(workers) == 1 and re.fullmatch(r"xbio-parser-\d{6}", workers.pop())
    w = owner.execute("select kind, layers from worker where id like 'xbio-parser-%'").fetchone()
    assert w["kind"] == "parser" and w["layers"] == ["public/x_ai"]
    run = owner.execute("select status, stats from run order by id desc limit 1").fetchone()
    assert run["status"] == "ok" and run["stats"]["api_calls"] == 1 and run["stats"]["cost_usd"] > 0
    stored = owner.execute("select count(*) n from claim where layer_id = 'public/x_ai' and evidence is null")
    assert stored.fetchone()["n"] == 0
    # resolver-made name claims (no evidence) never happen: extract names every new entity itself
    assert not [c for c in sent if not c["source_ref"].startswith("x_parse:")]
    # SF is one place, matched by its Wikidata id across bios
    sf = owner.execute(
        "select count(distinct subject) n from claim where predicate = 'wikidata' and value_text = 'Q62'"
    )
    assert sf.fetchone()["n"] <= 1
    places = owner.execute(
        "select count(distinct object_id) n from claim where predicate = 'based_in'"
    ).fetchone()
    assert places["n"] == 1


def test_resync_skips_parsed_and_reparses_changed(deps, owner):
    inputs = [(ext, p) for s, ext, p in fixture_inputs("x_parse/bios.json") if s == "x_spider"]
    seed_upstream(deps, "x_spider", inputs)
    model = recorded_model("x_parse/bios.json")
    run_sync(XParse(model=model, raw=deps.raw), with_env(deps))
    assert sum(len(c) for c in model.calls) == 6

    run_sync(XParse(model=model, raw=deps.raw), with_env(deps))
    assert sum(len(c) for c in model.calls) == 6  # nothing new: no calls

    # The spider re-fetches one profile with a changed bio -> only that one is parsed again.
    ext, payload = inputs[1]
    changed = {**payload, "user": {**payload["user"], "description": payload["user"]["description"] + " "}}
    changed["user"]["location"] = "SF "  # whitespace-only change: same input text, new hash
    seed_upstream(deps, "x_spider", [(ext, changed)], start=T0 + timedelta(hours=1))
    run_sync(XParse(model=model, raw=deps.raw), with_env(deps))
    assert sum(len(c) for c in model.calls) == 6  # same text after cleaning: stored output reused

    changed["user"]["description"] = "Partner @harborvc_ex"
    model.outputs["Marc Bell\nSF\nPartner @harborvc_ex"] = {"account": "person", "facts": []}
    seed_upstream(deps, "x_spider", [(ext, changed)], start=T0 + timedelta(hours=2))
    run_sync(XParse(model=model, raw=deps.raw), with_env(deps))
    assert model.calls[-1] == [f"x_spider:{ext}"]


def test_prompt_version_change_reparses(deps, owner, monkeypatch):
    seed_upstream(
        deps, "x_spider", [(ext, p) for s, ext, p in fixture_inputs("x_parse/bios.json") if s == "x_spider"]
    )
    model = recorded_model("x_parse/bios.json")
    run_sync(XParse(model=model, raw=deps.raw), with_env(deps))
    from rebox_worker.parsers import base

    monkeypatch.setattr(base, "PROMPT_VERSION", "test-next")
    run_sync(XParse(model=model, raw=deps.raw), with_env(deps))
    assert sum(len(c) for c in model.calls) == 12


def test_budget_and_limit(deps, owner):
    seed_upstream(
        deps, "x_spider", [(ext, p) for s, ext, p in fixture_inputs("x_parse/bios.json") if s == "x_spider"]
    )
    model = recorded_model("x_parse/bios.json")
    run_sync(
        XParse(model=model, raw=deps.raw), with_env(deps, PARSER_BATCH="2", PARSER_MAX_CALLS_PER_RUN="1")
    )
    assert [len(c) for c in model.calls] == [2]
    cursor = owner.execute("select cursor from connector_state where source = 'x_parse'").fetchone()["cursor"]
    assert cursor["after"][2] == "bios:1"  # resumes after the last parsed input

    deps.ctx.limit = 3
    run_sync(XParse(model=model, raw=deps.raw), with_env(deps, PARSER_BATCH="2"))
    assert [len(c) for c in model.calls] == [2, 2, 1]  # --limit 3 inputs
    run_sync(XParse(model=model, raw=deps.raw), with_env(deps, PARSER_BATCH="10"))
    assert sum(len(c) for c in model.calls) == 6  # the rest, nothing twice


def test_replay_makes_zero_model_calls(deps, owner):
    seed_upstream(deps, "x_spider", [(ext, p) for s, ext, p in fixture_inputs("x_parse/posts.json")])
    seed_upstream(deps, "x_spider", [(ext, p) for s, ext, p in fixture_inputs("x_parse/bios.json") if s == "x_spider"],
                  start=T0 - timedelta(days=1))  # fmt: skip
    model = recorded_model("x_parse/posts.json", "x_parse/bios.json")
    stats = run_sync(XParsePosts(model=model, raw=deps.raw), with_env(deps))
    assert stats.rejected == 0 and stats.written > 0 and model.calls
    claims = owner.execute("select count(*) n from claim").fetchone()["n"]
    entities = owner.execute("select count(*) n from entity").fetchone()["n"]

    class NoModel:
        name = "claude-haiku-5-5"

        def parse(self, docs, usage):
            raise AssertionError("replay must not call the model")

    replay = run_replay(XParsePosts(model=NoModel(), raw=deps.raw), deps)
    assert replay.fetched == stats.fetched and replay.rejected == 0 and replay.minted == 0
    assert owner.execute("select count(*) n from claim").fetchone()["n"] == claims
    assert owner.execute("select count(*) n from entity").fetchone()["n"] == entities
    milestones = owner.execute(
        "select value, valid_from from claim where predicate = 'milestone' order by value->>'title'"
    ).fetchall()
    assert len(milestones) >= 6 and all(
        m["valid_from"].date().isoformat() == "2026-10-21" for m in milestones
    )
    assert {m["value"]["kind"] for m in milestones} >= {
        "launch",
        "funding",
        "hire",
        "acquisition",
        "product",
        "award",
    }


def test_posts_link_to_accounts_already_in_the_graph(deps, owner):
    """A company posting about itself lands on the entity the spider made (by x_id/x_handle)."""

    # The company account exists from a deterministic connector (as x_spider would make it).
    class Spider:
        name, layer, interval_s = "x_spider", "public/x_spider", 3600

        def fetch(self, ctx, cursor):
            yield RawRecord.new(
                "x_spider", "test:tally", {"handle": "tallyhq_ex"}, fetched_at=T0 - timedelta(days=2)
            )

        def extract(self, rec):
            ref = {"kind": "company", "identity": {"x_handle": "tallyhq_ex"}, "name": "Tally"}
            return [ClaimInput(subject=ref, predicate="name", value="Tally", source_ref="x_spider:test:name"),
                    ClaimInput(subject=ref, predicate="x_handle", value="tallyhq_ex", source_ref="x_spider:test:h")]  # fmt: skip

    run_sync(Spider(), deps)
    tally = owner.execute("select subject from claim where source_ref = 'x_spider:test:name'").fetchone()[
        "subject"
    ]

    seed_upstream(deps, "x_spider", [(ext, p) for s, ext, p in fixture_inputs("x_parse/posts.json")])
    run_sync(XParsePosts(model=recorded_model("x_parse/posts.json"), raw=deps.raw), with_env(deps))
    kinds = {r["value"]["kind"] for r in owner.execute(
        "select value from claim where subject = %s and predicate = 'milestone'", (tally,))}  # fmt: skip
    assert {"launch", "funding", "product", "award", "hire"} <= kinds
    # no second "Tally" company
    n = owner.execute(
        "select count(distinct subject) n from claim where predicate = 'name' and value_text = 'Tally'"
    ).fetchone()["n"]
    assert n == 1


def test_github_and_exa_parsers(deps, owner):
    data = load_recorded("x_parse/bios.json")
    gh = [
        (f"user:{it['payload']['user']['login']}", it["payload"])
        for it in data["inputs"]
        if it["source"] == "github"
    ]
    seed_upstream(deps, "github", gh)
    seed_upstream(deps, "exa", [(ext, p) for s, ext, p in fixture_inputs("exa_parse/articles.json")])
    model = recorded_model("x_parse/bios.json", "exa_parse/articles.json")
    g = run_sync(GitHubParse(model=model, raw=deps.raw), with_env(deps))
    e = run_sync(ExaParse(model=model, raw=deps.raw), with_env(deps))
    assert g.rejected == 0 and e.rejected == 0
    layers = {
        r["layer_id"]: r["n"] for r in owner.execute("select layer_id, count(*) n from claim group by 1")
    }
    assert layers.get("public/github_ai", 0) > 0 and layers.get("public/exa_ai", 0) > 0
    acq = owner.execute("select subject, object_id from claim where predicate = 'acquired'").fetchall()
    assert len(acq) == 1


def test_fetch_yields_cursor_after_records():
    """A CursorUpdate always follows the records it covers (pipeline flush contract)."""

    class FakeDb:
        def __init__(self, rows):
            self.rows = rows

        def view(self):
            db = self

            class Conn:
                def execute(self, sql, params=None):
                    class R:
                        def __init__(self, rows):
                            self.rows = rows

                        def fetchall(self):
                            return self.rows

                        def __iter__(self):
                            return iter(self.rows)

                    if "from raw_record" in sql and "source = any" in sql:
                        out, db.rows = db.rows, []
                        return R(out)
                    return R([])

            from contextlib import contextmanager

            @contextmanager
            def cm():
                yield Conn()

            return cm()

    class Store:
        def get(self, key):
            return RawRecord.new("x_spider", key, {"type": "x_user", "user": {"id": "1", "username": "a_ex",
                                 "name": "A", "description": "CTO at Gridly", "location": ""}}, fetched_at=T0)  # fmt: skip

    rows = [{"source": "x_spider", "external_id": f"u{i}", "store_key": f"u{i}", "content_hash": "h", "fetched_at": T0}
            for i in range(3)]  # fmt: skip
    model = RecordedModel({}, strict=False)
    items = list(
        XParse(model=model, raw=Store()).fetch(Ctx(db=FakeDb(rows), env={"PARSER_BATCH": "2"}), None)
    )
    kinds = ["R" if isinstance(i, RawRecord) else "C" for i in items]
    assert kinds[-1] == "C" and kinds.index("C") == 2  # 2 records, cursor, ...
    assert isinstance(items[-1], CursorUpdate) and items[-1].cursor["after"][2] == "u2"


def test_exa_replay_applies_relevance_with_no_model_calls(deps, owner):
    """S18: the relevance filter is a post-filter on stored model output, so a replay applies it;
    the run's stats count what it dropped."""

    def stored(ref: str, title: str, text: str, facts: list[dict[str, Any]]) -> RawRecord:
        payload = {"input_ref": ref, "input_hash": "", "model": "claude-haiku-5-5", "prompt_version": PROMPT_VERSION,
                   "input": {"kind": "article", "fields": {"title": title, "text": text},
                             "meta": {"date": "2026-09-05", "url": f"https://news.example/{ref}", "author": "news.example",
                                      "host": "news.example"}},
                   "output": {"account": "other", "facts": facts}, "graph": {}}  # fmt: skip
        return RawRecord.new("exa_parse", ref, payload, fetched_at=T0)

    ms = {"type": "milestone", "kind": "funding", "title": "Known raised $9.7M", "org": "Known", "org_handle": "",
          "first_person": False, "quote": "Known raised $9.7M"}  # fmt: skip
    on_topic = stored(
        "a1", "Known raises $9.7M for voice AI dating", "Known raised $9.7M from Forerunner.", [ms]
    )
    listicle = stored("a2", "The Waggle: hot girls in SF this week", "Fog, tacos and parties. " * 30 + "Known raised $9.7M last year.", [ms])  # fmt: skip
    for rec in (on_topic, listicle):
        deps.raw.put(rec)

    class NoModel:
        name = "claude-haiku-5-5"

        def parse(self, docs, usage):
            raise AssertionError("replay must not call the model")

    stats = run_replay(ExaParse(model=NoModel(), raw=deps.raw), deps)
    assert stats.fetched == 2 and stats.rejected == 0
    assert stats.usage["irrelevant_dropped"] == 1 and stats.usage["duplicate_milestones"] == 0
    rows = owner.execute("select value from claim where predicate = 'milestone'").fetchall()
    assert [r["value"]["url"] for r in rows] == ["https://news.example/a1"]
    run = owner.execute("select kind, stats from run order by id desc limit 1").fetchone()
    assert run["kind"] == "replay" and run["stats"]["irrelevant_dropped"] == 1
