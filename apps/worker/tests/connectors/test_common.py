"""Rules every connector follows (spec "Rules for every connector"), checked on all fixtures,
plus the pipeline end to end on fixtures: Wikidata + HN -> one company per domain."""

from __future__ import annotations

import httpx
import pytest

from rebox_worker.connectors import get_connector
from rebox_worker.connectors.github import GitHub
from rebox_worker.connectors.x import X
from rebox_worker.models import RawRecord
from rebox_worker.pipeline import run_replay, run_sync

from .conftest import FIXTURES, ctx, records
from .test_github_x import _github_api, _graph, _x_api


def _all_records() -> list[tuple[str, RawRecord]]:
    out = [(s, r) for s in ("wikidata", "hn") for r in records(s)]
    handler, _ = _github_api()
    gh = GitHub(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    out += [
        (gh.name, r)
        for r in gh.fetch(ctx(env={"GITHUB_TOKEN": "fake-token"}), None)
        if isinstance(r, RawRecord)
    ]
    handler, _ = _x_api()
    x = X(transport=httpx.MockTransport(handler), sleep=lambda s: None, handles=_graph)
    out += [
        (x.name, r)
        for r in x.fetch(ctx(env={"X_BEARER_TOKEN": "fake-token"}), None)
        if isinstance(r, RawRecord)
    ]
    return out


ALL = _all_records()


@pytest.fixture(scope="module")
def vocab():
    """The predicate vocabulary, parsed from the seed SQL (no DB needed)."""
    import json
    import re

    sql = (FIXTURES.parents[1] / "db" / "seed" / "0001_vocabulary.sql").read_text()
    rows = re.findall(
        r"\('([a-z_]+)',\s*'\{([a-z,]+)\}',\s*(null|'[a-z]+'),\s*'(one|many)',\s*'(\{.*?\})'\)", sql
    )
    return {
        pid: {
            "kinds": set(kinds.split(",")),
            "object": None if ok == "null" else ok.strip("'"),
            "meta": json.loads(meta),
        }
        for pid, kinds, ok, _card, meta in rows
    }


def test_vocab_parsed(vocab):
    assert len(vocab) >= 25 and vocab["email"]["meta"]["private"] is True


@pytest.mark.parametrize(("source", "rec"), ALL, ids=[f"{s}:{r.external_id}" for s, r in ALL])
def test_claims_fit_the_vocabulary_and_rules(source, rec, vocab):
    claims = get_connector(source).extract(rec)
    names = set()
    for c in claims:
        p = vocab[c.predicate]
        assert not p["meta"].get("private"), f"private predicate {c.predicate}"
        assert c.subject.kind in p["kinds"]
        assert c.source_ref.startswith(f"{source}:") and " " not in c.source_ref.split(":", 2)[1]
        if p["object"]:
            assert c.object is not None and c.object.kind == p["object"]
            assert c.value is None or isinstance(c.value, dict)
        else:
            assert c.object is None and c.value is not None and not isinstance(c.value, dict)
        if c.predicate == "name":
            names.add((c.subject.kind, c.subject.name))
        for ref in (c.subject, c.object):
            if ref is not None:
                for key in ref.identity:
                    assert vocab[key]["meta"].get("identity"), key
    # Every entity that gets facts has a name (X enriches existing entities by handle only).
    if source != "x":
        described = {(c.subject.kind, c.subject.name) for c in claims}
        assert described <= names | {(k, None) for k, _ in described}


def test_no_emails_in_fixtures():
    import re

    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[a-z]{2,}")
    for path in FIXTURES.glob("*/**/*.json"):
        if path.parts[-2] != "example" and "example" not in path.parent.name:
            assert not email.findall(path.read_text()), path


# ── pipeline on fixtures (fake writer, test DB) ────────────────────────────


class _FromFixtures:
    """Wrap a connector so fetch replays its recorded fixtures (no network)."""

    def __init__(self, source: str):
        self.inner = get_connector(source)
        self.name, self.layer, self.interval_s = self.inner.name, self.inner.layer, self.inner.interval_s

    def fetch(self, ctx, cursor):
        yield from records(self.name)

    def extract(self, rec):
        return self.inner.extract(rec)


def _entities_with(owner, predicate: str, *values: str) -> int:
    return owner.execute(
        "select count(distinct subject) n from claim where predicate = %s and value_text = any(%s)",
        (predicate, list(values)),
    ).fetchone()["n"]


def test_pipeline_dedupes_companies_across_wikidata_and_hn(deps, owner):
    wd = run_sync(_FromFixtures("wikidata"), deps)
    hn = run_sync(_FromFixtures("hn"), deps)
    assert wd.rejected == 0 and hn.rejected == 0 and wd.extract_errors == 0 and hn.extract_errors == 0
    # Companies with a domain on both sources are one entity with facts in both layers.
    rows = owner.execute(
        """select d.value_text as domain, count(distinct d.subject) as entities,
                  array_agg(distinct c.layer_id order by c.layer_id) as layers
             from claim d join claim c on c.subject = d.subject
            where d.predicate = 'domain'
            group by d.value_text
           having count(distinct d.layer_id) = 2"""
    ).fetchall()
    shared = {r["domain"]: r for r in rows}
    assert {
        "brex.com",
        "deepgram.com",
        "resend.com",
        "termius.com",
        "buymeacoffee.com",
        "getnao.io",
        "didit.me",
    } <= set(shared)
    assert all(r["entities"] == 1 and r["layers"] == ["public/hn", "public/wikidata"] for r in rows)
    # Brex: Wikidata name + founded_year, HN yc_batch + launched, on one entity.
    brex = owner.execute(
        "select distinct subject from claim where predicate = 'domain' and value_text = 'brex.com'"
    ).fetchone()["subject"]
    preds = {
        (r["layer_id"], r["predicate"])
        for r in owner.execute("select layer_id, predicate from claim where subject = %s", (brex,))
    }
    assert {
        ("public/wikidata", "founded_year"),
        ("public/hn", "yc_batch"),
        ("public/hn", "launched"),
    } <= preds
    # GitHub and GitHub, Inc. stay two entities (shared website + handle are not identity).
    assert _entities_with(owner, "wikidata", "Q364", "Q28771536") == 2
    # Shared founders across two Wikidata orgs are one person (by QID).
    assert _entities_with(owner, "wikidata", "Q16602077") == 1
    # The same HN author's two Slauth launches -> one person, one company.
    slauth = owner.execute(
        "select count(distinct subject) n from claim where predicate = 'domain' and value_text = 'slauth.io'"
    ).fetchone()
    assert slauth["n"] == 1
    assert owner.execute("select count(*) n from review_item").fetchone()["n"] == 0


def test_replay_creates_no_new_entities(deps, owner):
    run_sync(_FromFixtures("wikidata"), deps)
    run_sync(_FromFixtures("hn"), deps)
    before = owner.execute("select count(*) n from entity").fetchone()["n"]
    claims = owner.execute("select count(*) n from claim").fetchone()["n"]
    for source in ("hn", "wikidata"):
        stats = run_replay(get_connector(source), deps)
        assert stats.minted == 0 and stats.rejected == 0
    assert owner.execute("select count(*) n from entity").fetchone()["n"] == before
    assert owner.execute("select count(*) n from claim").fetchone()["n"] == claims


def test_x_enriches_entities_from_github(deps, owner):
    """GitHub (synthetic) puts x_handles in the graph; X enriches those entities in place."""
    handler, _ = _github_api()
    gh = GitHub(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    deps.ctx.env = {"GITHUB_TOKEN": "fake-token", "X_BEARER_TOKEN": "fake-token"}
    assert run_sync(gh, deps).rejected == 0
    entities = owner.execute("select count(*) n from entity").fetchone()["n"]

    # Default handle source: graph_handles(ctx) reads the graph through ctx.db (the worker's pool).
    handler, _ = _x_api()
    x = X(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert deps.ctx.db is None  # the pipeline fills it in from deps.db
    stats = run_sync(x, deps)
    assert stats.minted == 0 and stats.rejected == 0 and stats.written > 0
    assert owner.execute("select count(*) n from entity").fetchone()["n"] == entities
    row = owner.execute(
        """select f.value_text as followers, n.value_text as name from claim f
             join claim n on n.subject = f.subject and n.predicate = 'name'
            where f.predicate = 'x_followers' and f.layer_id = 'public/x' and n.layer_id = 'public/github'
              and n.value_text = 'Example Octo One'"""
    ).fetchone()
    assert row["followers"] == "1234"
    assert not owner.execute(
        "select 1 from claim where layer_id = 'public/x' and predicate = 'name'"
    ).fetchall()
