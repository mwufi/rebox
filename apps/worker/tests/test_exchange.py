"""Graph merge between Rebox instances: export -> bundle -> import through the resolver (S11).

Two databases: the session test database is the **source** instance, ``<S2_TEST_DB>_tgt`` the
**target**. The end-to-end tests run the real API (``apps/api`` via its local ``tsx``) on each,
so worker registration with an explicit id, the writer's upsert and the fold are the real ones;
they are skipped if the API's node_modules are not installed. The dry-run and hint tests need
only the databases (the target is seeded with the in-process fake writer).
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from fake_writer import FakeWriter
from psycopg.rows import dict_row

from rebox_worker import cli
from rebox_worker.client import ClaimsClient
from rebox_worker.db import Db
from rebox_worker.exchange import (
    ImportAborted,
    ImportOptions,
    export_layers,
    import_bundle,
    iter_claims,
    read_header,
)
from rebox_worker.models import ClaimInput, EntityRef, Evidence
from rebox_worker.resolve import Resolver

REPO = Path(__file__).resolve().parents[3]
API_DIR = REPO / "apps" / "api"
TARGET_DB = os.environ.get("S2_TEST_DB", "s2_pytest") + "_tgt"
TABLES = (
    "claim, entity, change, job, connector_state, raw_record, resolution_log, review_item, merge_log,"
    " run, worker"
)
SEEDED_LAYERS = ("public/manual", "public/wikidata", "public/hn", "public/github", "public/x")
TOKEN = "test-ingest-token"
LAYERS = ["public/xa", "public/xs"]


# ── fixtures: a second (target) database and an API on each ──


@pytest.fixture(scope="session")
def target_urls() -> tuple[str, str]:
    assert TARGET_DB.startswith("s2_"), "S2 tests only use s2_ databases"
    out = subprocess.run(
        [str(REPO / "scripts" / "test-db.sh"), TARGET_DB], check=True, capture_output=True, text=True
    ).stdout.split()
    return out[0], out[1]


@pytest.fixture
def tgt_owner(target_urls) -> Iterator[psycopg.Connection]:
    with psycopg.connect(target_urls[0], autocommit=True, row_factory=dict_row) as conn:
        conn.execute(f"truncate {TABLES} restart identity cascade")
        conn.execute("delete from layer where id <> all(%s)", (list(SEEDED_LAYERS),))
        yield conn


@pytest.fixture
def tgt_db(target_urls, tgt_owner) -> Iterator[Db]:
    d = Db(target_urls[1], max_size=4)
    yield d
    d.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_api(app_url: str) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": app_url,
        "PORT": str(port),
        "INGEST_TOKEN": TOKEN,
        "ADMIN_TOKEN": "test-admin-token",
        "REBOX_ENV": "dev",
    }
    proc = subprocess.Popen(
        [str(API_DIR / "node_modules" / ".bin" / "tsx"), "src/server.ts"],
        cwd=API_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while True:
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                return proc, url
        except httpx.TransportError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.terminate()
            raise RuntimeError(
                f"api did not start: {proc.stderr.read().decode()[-2000:] if proc.stderr else ''}"
            )
        time.sleep(0.3)


@pytest.fixture(scope="module")
def apis(app_url: str, target_urls) -> Iterator[tuple[str, str]]:
    """(source API url, target API url)."""
    if not (API_DIR / "node_modules" / ".bin" / "tsx").exists():
        pytest.skip("apps/api/node_modules not installed (pnpm install)")
    procs = []
    try:
        src, src_url = _start_api(app_url)
        procs.append(src)
        tgt, tgt_url = _start_api(target_urls[1])
        procs.append(tgt)
        yield src_url, tgt_url
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)


@pytest.fixture
def src_client(apis) -> Iterator[ClaimsClient]:
    c = ClaimsClient(apis[0], TOKEN, backoff_s=0.01, max_backoff_s=0.05)
    yield c
    c.close()


@pytest.fixture
def tgt_client(apis) -> Iterator[ClaimsClient]:
    c = ClaimsClient(apis[1], TOKEN, backoff_s=0.01, max_backoff_s=0.05)
    yield c
    c.close()


# ── data ──


def write_as(
    client: ClaimsClient,
    db: Db,
    *,
    name: str,
    kind: str,
    source: str,
    layer: str,
    batches: list[list[ClaimInput]],
    config: dict | None = None,
) -> str:
    """Resolve and write ``batches`` (one resolver batch each) as one worker, in one run."""
    wid = client.register_worker(name=name, kind=kind, source=source, layers=[layer], config=config)
    run = client.open_run(wid, "sync")
    resolver = Resolver(db, source=source, layer=layer)
    for claims in batches:
        res = resolver.resolve(claims)
        assert not res.rejected, res.rejected
        client.write(layer, res.claims, worker=wid, run_id=run)
        resolver.commit(res)
    client.update_run(run, status="ok")
    return wid


def person(handle: str, xid: str, name: str | None = None) -> EntityRef:
    return EntityRef(kind="person", identity={"x_id": xid, "x_handle": handle}, name=name)


def ev(handle: str, quote: str) -> list[Evidence]:
    return [
        Evidence(url=f"https://x.com/{handle}", author=f"@{handle}", quote=quote, observed_at="2026-11-02")
    ]


def seed_source(client: ClaimsClient, db: Db) -> dict[str, str]:
    """Two layers like tonight's: an X spider (public/xs) and a bio parser with evidence (public/xa)."""
    ada, bo = person("ada", "1", "Ada Lovelace"), person("bo", "2", "Bo")
    acme = EntityRef(
        kind="company", identity={"x_id": "3", "x_handle": "acme", "domain": "acme.ai"}, name="Acme"
    )

    def user(ref: EntityRef, uid: str, **attrs):
        out = [
            ClaimInput(subject=ref, predicate=p, value=v, source_ref=f"xs:user:{uid}:{p}")
            for p, v in {"x_id": uid, "x_handle": ref.identity["x_handle"], "name": ref.name, **attrs}.items()
        ]
        return out

    spider = write_as(
        client,
        db,
        name="seedx",
        kind="spider",
        source="xs",
        layer="public/xs",
        config={"seeds": ["ada"]},
        batches=[
            user(ada, "1", headline="First programmer", website="https://a16z.com", x_followers=120)
            + user(bo, "2", website="https://a16z.com")
            + user(acme, "3", domain="acme.ai", website="https://acme.ai")
            + [
                ClaimInput(
                    subject=bo,
                    predicate="headline",
                    value="old bio",
                    retract=True,
                    source_ref="xs:user:2:old",
                ),
                ClaimInput(
                    subject=acme,
                    predicate="milestone",
                    value={"title": "Seed round", "kind": "funding", "url": "https://acme.ai/seed"},
                    valid_from="2024-03-01",
                    source_ref="xs:post:9:milestone",
                ),
            ],
            [
                ClaimInput(
                    subject=person("ada", "1"), predicate="follows", object=o, source_ref=f"xs:follows:1:{i}"
                )
                for i, o in (("2", person("bo", "2")), ("3", acme.model_copy(update={"name": None})))
            ],
        ],
    )
    # The parser: identity-less "KV" twice (two different source entities with the same name), and a
    # company minted from an X handle it never writes as a claim (its key lives in resolution_log).
    kv = EntityRef(kind="company", name="KV")
    stripe = EntityRef(kind="company", identity={"x_handle": "stripe"}, name="Stripe")
    parser = write_as(
        client,
        db,
        name="xbio",
        kind="parser",
        source="xa",
        layer="public/xa",
        batches=[
            [
                ClaimInput(
                    subject=ada,
                    predicate="worked_at",
                    object=kv,
                    value={"title": "Partner"},
                    valid_from="2020-01-01",
                    source_ref="xa:user:1:worked_at:kv",
                    evidence=ev("ada", "Partner @ KV"),
                ),
                ClaimInput(
                    subject=stripe,
                    predicate="name",
                    value="Stripe",
                    source_ref="xa:user:2:name:stripe",
                    evidence=ev("bo", "Led docs at @stripe"),
                ),
                ClaimInput(
                    subject=bo,
                    predicate="worked_at",
                    object=stripe,
                    value={"title": "Docs"},
                    source_ref="xa:user:2:worked_at:stripe",
                    evidence=ev("bo", "Led docs at @stripe"),
                ),
            ],
            [
                ClaimInput(
                    subject=bo,
                    predicate="worked_at",
                    object=kv,
                    source_ref="xa:user:2:worked_at:kv",
                    evidence=ev("bo", "ex-KV"),
                )
            ],
        ],
    )
    return {"spider": spider, "parser": parser}


def entity_ids(owner_url: str, layers: list[str]) -> list[str]:
    with psycopg.connect(owner_url) as conn:
        rows = conn.execute(
            """select subject from claim where layer_id = any(%s)
               union select object_id from claim where layer_id = any(%s) and object_id is not null""",
            (layers, layers),
        ).fetchall()
    return sorted(r[0] for r in rows)


def view(api: str, eid: str) -> dict:
    """The folded entity view, without updated_at and without the winning claim's source_ref (when
    several claims back the same fact with equal trust and time, which one is shown depends on
    insertion order; the claims themselves are compared exactly by ``claims_of``)."""
    v = httpx.get(f"{api}/v0/entities/{eid}").json()

    def norm(f: dict) -> tuple:
        return (
            f["predicate"],
            json.dumps(f["value"], sort_keys=True),
            (f["object"] or {}).get("id"),
            f["subject"]["id"],
            f["valid_from"],
            f["valid_to"],
            f["layer"],
            f["source"],
            f["worker"],
            json.dumps(f["evidence"], sort_keys=True),
            f["support"],
        )

    return {
        "id": v["id"],
        "kind": v["kind"],
        "name": v["name"],
        "facts": sorted(norm(f) for f in v["facts"]),
        "incoming": sorted(norm(f) for f in v["incoming"]),
    }


def claims_of(owner_url: str, layers: list[str]) -> list[tuple]:
    with psycopg.connect(owner_url) as conn:
        return conn.execute(
            """select layer_id, source_ref, subject, predicate, object_id, value::text, valid_from, valid_to,
                      retract, worker_id, evidence::text
                 from claim where layer_id = any(%s) order by 1, 2, 3, 4, 5""",
            (layers,),
        ).fetchall()


def counts(owner_url: str) -> dict:
    with psycopg.connect(owner_url, row_factory=dict_row) as conn:
        return conn.execute(
            """select (select count(*) from claim) as claims, (select count(*) from entity) as entities,
                      (select coalesce(max(seq), 0) from change) as change_seq,
                      (select count(*) from worker) as workers, (select count(*) from run) as runs,
                      (select count(*) from resolution_log) as rlog,
                      (select count(*) from review_item) as reviews, (select count(*) from layer) as layers"""
        ).fetchone()


@pytest.fixture
def bundle(db, src_client, tmp_path) -> tuple[Path, dict[str, str]]:
    workers = seed_source(src_client, db)
    path = tmp_path / "b.rebox.jsonl.gz"
    export_layers(db, LAYERS, path, source_instance="overnight")
    return path, workers


# ── export ──


def test_export_bundle_format(bundle, owner_url):
    path, workers = bundle
    header = read_header(path)
    assert header["version"] == 1 and header["source_instance"] == "overnight" and header["layers"] == LAYERS
    assert {w["id"]: (w["kind"], w["source"], w["layers"]) for w in header["workers"]} == {
        workers["spider"]: ("spider", "xs", ["public/xs"]),
        workers["parser"]: ("parser", "xa", ["public/xa"]),
    }
    claims = list(iter_claims(path))
    with psycopg.connect(owner_url) as conn:
        n = conn.execute("select count(*) from claim where layer_id = any(%s)", (LAYERS,)).fetchone()[0]
    assert len(claims) == n == sum(header["claims"].values())
    # Deterministic: grouped by layer and worker, and the same bytes on a second export.
    keys = [(c["layer"], c["worker"]) for c in claims]
    assert keys == sorted(keys)
    stripe = next(c for c in claims if c["source_ref"] == "xa:user:2:name:stripe")["subject"]
    # Stripe has no x_handle claim anywhere: its identity comes from the source's resolution_log.
    assert stripe["identity"] == {"x_handle": "stripe"} and stripe["origin_id"].startswith("rbx:company:")
    ada = next(c for c in claims if c["source_ref"] == "xs:user:1:name")["subject"]
    assert ada["identity"] == {"website": "https://a16z.com", "x_handle": "ada", "x_id": "1"}
    assert ada["name"] == "Ada Lovelace"
    kvs = {
        c["object"]["origin_id"]
        for c in claims
        if c["predicate"] == "worked_at" and c["object"]["name"] == "KV"
    }
    assert len(kvs) == 2  # two distinct identity-less entities on the source
    wk = next(c for c in claims if c["source_ref"] == "xa:user:1:worked_at:kv")
    assert wk["evidence"][0]["quote"] == "Partner @ KV" and wk["valid_from"].startswith("2020-01-01")
    assert any(c["retract"] for c in claims)


# ── import, end to end against the real API ──


def test_round_trip_into_empty_db_reproduces_entity_views(
    bundle, apis, owner_url, target_urls, tgt_db, monkeypatch, capsys
):
    path, workers = bundle
    monkeypatch.setenv("DATABASE_URL", target_urls[1])
    monkeypatch.setenv("API_URL", apis[1])
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    assert cli.main(["import", str(path)]) == 0
    out = capsys.readouterr().out
    assert "register (new)" in out and "run " in out

    ids = entity_ids(owner_url, LAYERS)
    assert ids == entity_ids(target_urls[0], LAYERS)  # same ids: same keys, origin id for identity-less
    assert claims_of(target_urls[0], LAYERS) == claims_of(owner_url, LAYERS)
    for eid in ids:
        assert view(apis[1], eid) == view(apis[0], eid), eid

    with psycopg.connect(target_urls[0], row_factory=dict_row) as conn:
        ws = {
            r["id"]: r
            for r in conn.execute("select id, kind, source, layers, config, trust from worker").fetchall()
        }
        runs = conn.execute("select worker_id, kind, status, stats from run order by id").fetchall()
        evidence = conn.execute(
            "select evidence from claim where source_ref = 'xa:user:1:worked_at:kv'"
        ).fetchone()["evidence"]
    assert set(ws) == set(workers.values())
    assert ws[workers["spider"]]["config"] == {"seeds": ["ada"]} and ws[workers["parser"]]["kind"] == "parser"
    assert {r["worker_id"] for r in runs} == set(workers.values())
    assert all(r["kind"] == "replay" and r["status"] == "ok" for r in runs)
    assert all(
        r["stats"]["written"] > 0 and r["stats"]["import"]["source_instance"] == "overnight" for r in runs
    )
    assert evidence == [
        {"url": "https://x.com/ada", "author": "@ada", "quote": "Partner @ KV", "observed_at": "2026-11-02"}
    ]


def test_import_merges_onto_existing_entities_with_other_ids(bundle, apis, tgt_db, tgt_client, target_urls):
    path, _ = bundle
    # Zen's graph: Ada minted from her GitHub login (another id), Stripe minted from its domain.
    gh = write_as(
        tgt_client,
        tgt_db,
        name="github",
        kind="connector",
        source="github",
        layer="public/github",
        batches=[
            [
                ClaimInput(
                    subject=EntityRef(
                        kind="person", identity={"github": "ada-gh", "x_handle": "@ADA"}, name="Ada L."
                    ),
                    predicate=p,
                    value=v,
                    source_ref=f"github:user:ada-gh:{p}",
                )
                for p, v in (("name", "Ada L."), ("github", "ada-gh"), ("x_handle", "ADA"))
            ]
            + [
                ClaimInput(
                    subject=EntityRef(
                        kind="company",
                        identity={"domain": "stripe.com", "x_handle": "stripe"},
                        name="Stripe, Inc.",
                    ),
                    predicate=p,
                    value=v,
                    source_ref=f"wd:stripe:{p}",
                )
                for p, v in (("name", "Stripe, Inc."), ("domain", "stripe.com"), ("x_handle", "stripe"))
            ]
        ],
    )
    with psycopg.connect(target_urls[0]) as conn:
        gh_ada, gh_stripe = (
            conn.execute(f"select subject from claim where source_ref = '{r}'").fetchone()[0]
            for r in ("github:user:ada-gh:github", "wd:stripe:domain")
        )
    report = import_bundle(path, tgt_db, tgt_client, ImportOptions())

    o = {x.name: x for x in report.outcomes.values()}
    assert (o["Ada Lovelace"].target, o["Ada Lovelace"].category, o["Ada Lovelace"].how) == (
        gh_ada,
        "matched_other_id",
        "x_handle=ada",
    )
    assert (o["Stripe"].target, o["Stripe"].how) == (gh_stripe, "x_handle=stripe")
    assert o["Ada Lovelace"].origin != gh_ada
    with psycopg.connect(target_urls[0]) as conn:
        persons = conn.execute(
            "select count(*) from entity where kind = 'person' and merged_into is null"
        ).fetchone()[0]
        assert persons == 2  # Ada (the GitHub entity) and Bo: no duplicate Ada
        assert (
            conn.execute("select count(*) from entity where id = %s", (o["Ada Lovelace"].origin,)).fetchone()[
                0
            ]
            == 0
        )
    ada = httpx.get(f"{apis[1]}/v0/entities/{gh_ada}").json()
    facts = {(f["predicate"], json.dumps(f["value"])) for f in ada["facts"]}
    assert (
        ("github", '"ada-gh"') in facts
        and ("x_id", '"1"') in facts
        and ("headline", '"First programmer"') in facts
    )
    assert any(f["predicate"] == "follows" for f in ada["facts"])
    assert any(f["predicate"] == "worked_at" and f["object"]["name"] == "KV" for f in ada["facts"])
    # A one-valued attribute that differs on the target is reported (and the incoming claim wins the tie).
    names = [c for c in report.conflicts if c.entity == gh_ada and c.predicate == "name"]
    assert names and names[0].target_value == "Ada L." and names[0].incoming_value == "Ada Lovelace"
    assert names[0].incoming_wins and ada["name"] == "Ada Lovelace"
    assert gh  # the target's own worker is untouched
    assert "x_handle=ada" in report.render()


def test_reimport_is_a_no_op(bundle, apis, tgt_db, target_urls):
    path, _ = bundle
    client = ClaimsClient(apis[1], TOKEN)
    first = import_bundle(path, tgt_db, client, ImportOptions(batch_claims=3))
    before = counts(target_urls[0])
    second = import_bundle(path, tgt_db, client, ImportOptions(batch_claims=7))
    client.close()
    assert counts(target_urls[0]) == before  # no claims, entities, runs, log rows or changes
    assert sum(lc.written for lc in first.layers.values()) > 0
    assert all(
        lc.unchanged == lc.claims and lc.new == lc.changed == lc.written == 0 for lc in second.layers.values()
    )
    assert all(w.run_id is None for w in second.workers)
    assert {o.category for o in second.outcomes.values()} == {"matched_same_id"}
    assert {o.origin: o.target for o in second.outcomes.values()} == {
        o.origin: o.target for o in first.outcomes.values()
    }


def test_dry_run_writes_nothing_and_predicts_the_import(bundle, apis, tgt_db, target_urls):
    path, _ = bundle
    before = counts(target_urls[0])
    dry = import_bundle(path, tgt_db, None, ImportOptions(dry_run=True, batch_claims=4))
    assert counts(target_urls[0]) == before
    client = ClaimsClient(apis[1], TOKEN)
    real = import_bundle(path, tgt_db, client, ImportOptions(batch_claims=4))
    client.close()
    assert dry.entity_counts() == real.entity_counts()
    assert {k: (v.claims, v.new) for k, v in dry.layers.items()} == {
        k: (v.claims, v.new) for k, v in real.layers.items()
    }
    assert {o.origin: o.target for o in dry.outcomes.values()} == {
        o.origin: o.target for o in real.outcomes.values()
    }
    assert "DRY RUN" in dry.render() and "DRY RUN" not in real.render()


def test_worker_id_with_another_source_aborts_before_writing(bundle, apis, tgt_db, tgt_owner, target_urls):
    path, workers = bundle
    tgt_owner.execute(
        "insert into worker (id, kind, source, layers)"
        " values (%s, 'spider', 'elsewhere', '{public/elsewhere}')",
        (workers["spider"],),
    )
    before = counts(target_urls[0])
    client = ClaimsClient(apis[1], TOKEN)
    with pytest.raises(ImportAborted, match="elsewhere"):
        import_bundle(path, tgt_db, client, ImportOptions())
    client.close()
    assert counts(target_urls[0]) == before
    dry = import_bundle(path, tgt_db, None, ImportOptions(dry_run=True))
    assert dry.aborted and "conflict" in dry.render()


# ── import, database only ──


def _write_bundle(path: Path, claims: list[dict], workers: list[dict]) -> None:
    header = {
        "type": "header",
        "version": 1,
        "source_instance": "handmade",
        "exported_at": "2026-11-02T00:00:00Z",
        "layers": sorted({c["layer"] for c in claims}),
        "workers": workers,
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for obj in [header, *({"type": "claim", **c} for c in claims)]:
            f.write(json.dumps(obj) + "\n")


def _ref(origin: str, name: str, **identity) -> dict:
    return {"kind": origin.split(":")[1], "identity": identity, "name": name, "origin_id": origin}


def test_shared_and_weak_hints_never_merge_distinct_entities(tmp_path, tgt_db, target_urls):
    """Two source people who share an X handle (a bad source) or a website stay two people; a
    website (weak key) only matches with --match-website."""
    w = FakeWriter(target_urls[1]).start()
    try:
        c = ClaimsClient(w.url, w.token)
        write_as(
            c,
            tgt_db,
            name="github",
            kind="connector",
            source="github",
            layer="public/github",
            batches=[
                [
                    ClaimInput(
                        subject=EntityRef(kind="person", identity={"github": "carol"}, name="Carol"),
                        predicate=p,
                        value=v,
                        source_ref=f"gh:carol:{p}",
                    )
                    for p, v in (("name", "Carol"), ("github", "carol"), ("website", "https://a16z.com"))
                ]
            ],
        )
        c.close()
    finally:
        w.stop()
    a = _ref("rbx:person:aaaa", "A", x_id="10", x_handle="same", website="https://a16z.com")
    b = _ref("rbx:person:bbbb", "B", x_id="11", x_handle="same", website="https://a16z.com")
    worker = {"id": "s-spider-000001", "kind": "spider", "source": "s", "trust": 0.8, "layers": ["public/s"]}
    claims = [
        {
            "layer": "public/s",
            "worker": worker["id"],
            "subject": r,
            "predicate": "name",
            "object": None,
            "value": r["name"],
            "retract": False,
            "source_ref": f"s:{r['name']}:name",
        }  # fmt: skip
        for r in (a, b)
    ]
    path = tmp_path / "h.rebox.jsonl.gz"
    _write_bundle(path, claims, [worker])
    before = counts(target_urls[0])
    report = import_bundle(path, tgt_db, None, ImportOptions(dry_run=True))
    assert counts(target_urls[0]) == before
    assert report.entity_counts()["person"]["minted"] == 2
    assert len({o.target for o in report.outcomes.values()}) == 2
    assert report.hints_dropped["x_handle (shared by several origin entities)"] == 2
    assert report.hints_dropped["website (weak key)"] == 2
    assert report.workers[0].action == "register (new)"
    # Only x_id is left, which is not a minting key: they keep their origin ids (stable across batches).
    assert {o.target for o in report.outcomes.values()} == {"rbx:person:aaaa", "rbx:person:bbbb"}
    # A website (unique in the bundle) matches Carol only when asked to.
    c = _ref("rbx:person:cccc", "C", x_handle="c", website="https://a16z.com")
    _write_bundle(path, [{**claims[0], "subject": c, "value": "C", "source_ref": "s:C:name"}], [worker])
    report = import_bundle(path, tgt_db, None, ImportOptions(dry_run=True))
    assert [o.category for o in report.outcomes.values()] == ["minted"]
    report = import_bundle(path, tgt_db, None, ImportOptions(dry_run=True, match_website=True))
    assert [(o.category, o.how) for o in report.outcomes.values()] == [
        ("matched_other_id", "website=https://a16z.com")
    ]


def test_identity_less_entities_keep_their_origin_id_and_never_collapse(tmp_path, tgt_db):
    kv1, kv2 = _ref("rbx:company:kv1", "KV"), _ref("rbx:company:kv2", "KV")
    worker = {"id": "p-parser-000001", "kind": "parser", "source": "p", "trust": 0.6, "layers": ["public/p"]}
    claims = [
        {
            "layer": "public/p",
            "worker": worker["id"],
            "subject": r,
            "predicate": "name",
            "object": None,
            "value": "KV",
            "retract": False,
            "source_ref": f"p:{r['origin_id']}:name",
            "evidence": [{"url": "https://x.com/a", "quote": "KV"}],
        }  # fmt: skip
        for r in (kv1, kv2)
    ]
    path = tmp_path / "kv.rebox.jsonl.gz"
    _write_bundle(path, claims, [worker])
    report = import_bundle(path, tgt_db, None, ImportOptions(dry_run=True, batch_claims=10))
    assert {o.origin: (o.target, o.decision) for o in report.outcomes.values()} == {
        "rbx:company:kv1": ("rbx:company:kv1", "minted"),
        "rbx:company:kv2": ("rbx:company:kv2", "minted"),
    }
    assert report.layers["public/p"].generated_names == 0  # the bundle names them itself


def test_cli_export_and_dry_run(bundle, tmp_path, app_url, target_urls, tgt_db, monkeypatch, capsys):
    path, _ = bundle
    monkeypatch.setenv("DATABASE_URL", app_url)
    out = tmp_path / "cli.rebox.jsonl.gz"
    assert (
        cli.main(["export", "--layers", ",".join(LAYERS), "--out", str(out), "--instance", "overnight"]) == 0
    )
    assert "exported" in capsys.readouterr().out

    def body(p: Path) -> list[str]:
        with gzip.open(p, "rt") as f:
            return f.readlines()[1:]

    assert body(out) == body(path)  # deterministic order and content
    monkeypatch.setenv("DATABASE_URL", target_urls[1])
    monkeypatch.delenv("API_URL", raising=False)
    assert cli.main(["import", str(out), "--dry-run", "--json", "--layers", "public/xs"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["dry_run"] and list(rep["layers"]) == ["public/xs"]
    assert rep["entities"]["person"]["minted"] == 2
    assert cli.main(["import", str(out), "--layers", "public/nope", "--dry-run"]) == 2


class _StubWriter:
    """Accepts everything except claims whose source_ref is in ``bad`` (a 400 naming them), like
    the real writer does for a value its normalizer disagrees on."""

    def __init__(self, bad: set[str]):
        self.bad = bad
        self.written: list[str] = []
        self.runs: dict[int, dict] = {}

    def _request(self, method, path, body):
        assert (method, path) == ("POST", "/internal/workers")
        return {"id": body["id"]}

    def open_run(self, worker, kind):
        self.runs[len(self.runs) + 1] = {"worker": worker, "kind": kind}
        return len(self.runs)

    def update_run(self, run_id, *, status=None, stats=None, error=None):
        self.runs[run_id].update(status=status or self.runs[run_id].get("status"), stats=stats)
        return {}

    def write_batch(self, layer, claims, *, worker, run_id=None):
        from rebox_worker.client import ValidationRejected, WriteResult

        details = [
            {"index": i, "message": "not normalized"}
            for i, c in enumerate(claims)
            if c.source_ref in self.bad
        ]
        if details:
            raise ValidationRejected("rejected", details)
        self.written += [c.source_ref for c in claims]
        return WriteResult(len(claims), 0)


def test_writer_rejection_drops_only_the_named_claims(tmp_path, tgt_db):
    a = _ref("rbx:person:aaaa", "A", x_handle="a")
    worker = {"id": "s-spider-000001", "kind": "spider", "source": "s", "trust": 0.8, "layers": ["public/s"]}
    base = {"layer": "public/s", "worker": worker["id"], "subject": a, "object": None, "retract": False}
    claims = [
        {**base, "predicate": "name", "value": "A", "source_ref": "s:a:name"},
        {**base, "predicate": "website", "value": "http://www.com", "source_ref": "s:a:website"},
        {**base, "predicate": "headline", "value": "hi", "source_ref": "s:a:headline"},
    ]
    path = tmp_path / "r.rebox.jsonl.gz"
    _write_bundle(path, claims, [worker])
    stub = _StubWriter({"s:a:website"})
    report = import_bundle(path, tgt_db, stub, ImportOptions())  # type: ignore[arg-type]
    assert sorted(stub.written) == ["s:a:headline", "s:a:name"]
    assert report.rejected == [("public/s", "s:a:website", "writer: not normalized")]
    lc = report.layers["public/s"]
    assert (lc.claims, lc.new, lc.written, lc.rejected) == (3, 3, 2, 1)
    assert stub.runs[1]["status"] == "ok" and stub.runs[1]["stats"]["rejected"] == 1


def test_ambiguous_match_prefers_the_bundles_own_id(tmp_path, tgt_db, target_urls):
    """The target holds two companies sharing an X handle; the imported one carries the QID of the
    newer one (and its id): it lands there, not on the oldest, and the ambiguity is reviewed."""
    w = FakeWriter(target_urls[1]).start()
    try:
        c = ClaimsClient(w.url, w.token)
        for ident, ref in (
            ({"domain": "moz.org", "x_handle": "moz"}, "a"),
            ({"wikidata": "Q2", "x_handle": "moz"}, "b"),
        ):
            # The second keeps the shared handle as a fact but not as identity (else it would merge).
            key = {k: v for k, v in ident.items() if not (ref == "b" and k == "x_handle")}
            company = EntityRef(kind="company", identity=key, name="Moz")
            write_as(c, tgt_db, name="wd", kind="connector", source="wd", layer="public/wd", batches=[[
                ClaimInput(subject=company, predicate=p, value=v, source_ref=f"wd:{ref}:{p}")
                for p, v in [("name", "Moz"), *ident.items()]
            ]])  # fmt: skip
        c.close()
    finally:
        w.stop()
    with psycopg.connect(target_urls[0]) as conn:
        older, newer = (
            conn.execute(f"select subject from claim where source_ref = 'wd:{r}:name'").fetchone()[0]
            for r in "ab"
        )
    moz = _ref(newer, "Mozilla", wikidata="Q2", x_handle="moz")
    worker = {"id": "p-parser-000001", "kind": "parser", "source": "p", "trust": 0.6, "layers": ["public/p"]}
    claim = {"layer": "public/p", "worker": worker["id"], "subject": moz, "predicate": "name", "object": None,
             "value": "Mozilla", "retract": False, "source_ref": "p:moz:name",
             "evidence": [{"url": "https://x.com/a", "quote": "@moz"}]}  # fmt: skip
    path = tmp_path / "m.rebox.jsonl.gz"
    _write_bundle(path, [claim], [worker])
    report = import_bundle(path, tgt_db, None, ImportOptions(dry_run=True))
    [o] = report.outcomes.values()
    assert (o.target, o.decision) == (newer, "review") and older != newer
