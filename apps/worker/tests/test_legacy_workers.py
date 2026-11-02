"""Migration 0008 (S17): legacy workers for claims written before worker identities.

The test database is already fully migrated, so these tests insert claims with no worker (as
every pre-0005 claim is) and run ``db/migrations/0008_legacy_workers.sql`` directly, the way
``db/migrate.sh`` does (one transaction, as the owner).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "db" / "migrations" / "0008_legacy_workers.sql"
LEGACY = {  # layer -> (worker id, trust from config/trust.yaml)
    "public/wikidata": ("wikidata-connector-000000", 0.9),
    "public/hn": ("hn-connector-000000", 0.7),
    "public/x_spider": ("x-spider-connector-000000", 0.75),
}


def migrate(owner_url: str) -> float:
    t = time.monotonic()
    subprocess.run(
        ["psql", owner_url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-1", "-f", str(MIGRATION)],
        check=True,
        capture_output=True,
        text=True,
    )
    return time.monotonic() - t


def seed_legacy(owner, per_layer: int) -> None:
    """``per_layer`` unattributed claims (names and descriptions) in each LEGACY layer, plus one
    layer written by a real worker, which the migration must leave alone."""
    owner.execute(
        """insert into layer (id, scope, source) values ('public/x_spider', 'public', 'x_spider')
           on conflict do nothing"""
    )
    for i, layer in enumerate(LEGACY):
        owner.execute(
            """insert into entity (id, kind, home_layer)
               select 'rbx:company:l' || %s || 'e' || g, 'company', %s from generate_series(1, %s) g""",
            (i, layer, (per_layer + 1) // 2),
        )
        owner.execute(
            """insert into claim (layer_id, subject, predicate, value, source_ref)
               select %s, 'rbx:company:l' || %s || 'e' || ((g + 1) / 2),
                      case when g %% 2 = 1 then 'name' else 'description' end,
                      to_jsonb('value ' || g), %s || ':' || g
                 from generate_series(1, %s) g""",
            (layer, i, layer, per_layer),
        )
    owner.execute(
        """insert into worker (id, kind, source, layers) values ('gh-connector-111111', 'connector', 'github',
           '{public/github}')"""
    )
    owner.execute(
        "insert into entity (id, kind, home_layer) values ('rbx:company:gh1', 'company', 'public/github')"
    )
    owner.execute(
        """insert into claim (layer_id, subject, predicate, value, source_ref, worker_id)
           values ('public/github', 'rbx:company:gh1', 'name', '"GH"', 'gh:1', 'gh-connector-111111')"""
    )


def snapshot(owner) -> dict:
    return {
        "workers": owner.execute(
            """select id, kind, source, owner, trust, layers, description, config, created_at
                 from worker order by id"""
        ).fetchall(),
        "claims": owner.execute(
            "select id, layer_id, worker_id, run_id, value, updated_at from claim order by id"
        ).fetchall(),
        "changes": owner.execute(
            "select seq, op, claim_id, worker_id, at from change order by seq"
        ).fetchall(),
    }


def test_legacy_claims_get_a_worker_per_source_without_change_rows(db, owner, owner_url):
    seed_legacy(owner, per_layer=20)
    before = snapshot(owner)
    assert sum(c["worker_id"] is None for c in before["claims"]) == 60
    assert sum(c["worker_id"] is None for c in before["changes"]) == 60  # the inserts' change rows

    migrate(owner_url)
    after = snapshot(owner)

    workers = {w["id"]: w for w in after["workers"]}
    assert set(workers) == {"gh-connector-111111", *(wid for wid, _ in LEGACY.values())}
    for layer, (wid, trust) in LEGACY.items():
        w = workers[wid]
        assert (w["kind"], w["owner"], w["layers"]) == ("connector", "rebox", [layer])
        assert w["source"] == layer.split("/", 1)[1]
        assert w["trust"] == pytest.approx(trust)
        assert w["config"] == {"trust_source": "table", "trust_table": trust}
        assert w["description"] == "claims written before worker identities (migration 0008)"

    # Every claim attributed to its layer's legacy worker; nothing else about the claims changed.
    by_layer = {layer: wid for layer, (wid, _) in LEGACY.items()} | {"public/github": "gh-connector-111111"}
    assert all(c["worker_id"] == by_layer[c["layer_id"]] for c in after["claims"])
    strip = lambda rows: [{k: v for k, v in r.items() if k != "worker_id"} for r in rows]  # noqa: E731
    assert strip(after["claims"]) == strip(before["claims"])

    # No change rows emitted; the old ones now name the legacy worker.
    assert [c["seq"] for c in after["changes"]] == [c["seq"] for c in before["changes"]]
    claim_worker = {c["id"]: c["worker_id"] for c in after["claims"]}
    assert all(ch["worker_id"] == claim_worker[ch["claim_id"]] for ch in after["changes"])

    # The trigger is back on.
    assert (
        owner.execute("select tgenabled from pg_trigger where tgname = 'claim_change'").fetchone()[
            "tgenabled"
        ]
        == "O"
    )
    owner.execute(
        """insert into claim (layer_id, subject, predicate, value, source_ref)
           values ('public/hn', 'rbx:company:l1e1', 'website', '"https://a.com"', 'hn:new')"""
    )
    assert owner.execute("select count(*) n from change").fetchone()["n"] == len(before["changes"]) + 1
    owner.execute("delete from change where claim_id = (select id from claim where source_ref = 'hn:new')")
    owner.execute("delete from claim where source_ref = 'hn:new'")
    owner.execute("delete from change where op = 'delete'")

    # A second run is a no-op.
    migrate(owner_url)
    assert snapshot(owner) == after


def test_existing_legacy_worker_gains_a_layer_and_other_identities_stay(db, owner, owner_url):
    """A legacy worker that exists already is reused (and gets the new layer); a same-source
    connector registered earlier keeps being the one POST /internal/workers returns."""
    owner.execute(
        """insert into worker (id, kind, source, trust, layers, created_at) values
           ('hn-connector-104233', 'connector', 'hn', 0.8, '{public/hn}', now() - interval '1 day'),
           ('hn-connector-000000', 'connector', 'hn', 0.8, '{public/hn}', now() - interval '1 hour')"""
    )
    owner.execute("insert into layer (id, scope, owner, source) values ('team/t1/hn', 'team', 't1', 'hn')")
    owner.execute("insert into entity (id, kind, home_layer) values ('rbx:person:p1', 'person', 'public/hn')")
    owner.execute(
        """insert into claim (layer_id, subject, predicate, value, source_ref) values
           ('public/hn', 'rbx:person:p1', 'name', '"pg"', 'hn:1'),
           ('team/t1/hn', 'rbx:person:p1', 'name', '"Paul"', 'hn:1')"""
    )
    migrate(owner_url)
    rows = owner.execute("select id, layers, trust from worker order by created_at").fetchall()
    assert [(r["id"], r["layers"]) for r in rows] == [
        ("hn-connector-104233", ["public/hn"]),
        ("hn-connector-000000", ["public/hn", "team/t1/hn"]),
    ]
    assert rows[1]["trust"] == pytest.approx(
        0.8
    )  # an existing worker's trust is not the migration's business
    assert {r["worker_id"] for r in owner.execute("select worker_id from claim")} == {"hn-connector-000000"}


def test_conflicting_worker_id_leaves_claims_unattributed(db, owner, owner_url):
    owner.execute(
        """insert into worker (id, kind, source, layers)
           values ('hn-connector-000000', 'connector', 'other', '{public/x}')"""
    )
    owner.execute("insert into entity (id, kind, home_layer) values ('rbx:person:p1', 'person', 'public/hn')")
    owner.execute(
        """insert into claim (layer_id, subject, predicate, value, source_ref)
           values ('public/hn', 'rbx:person:p1', 'name', '"pg"', 'hn:1')"""
    )
    migrate(owner_url)
    assert owner.execute("select worker_id from claim").fetchone()["worker_id"] is None
    assert owner.execute("select source, layers from worker").fetchone() == {
        "source": "other",
        "layers": ["public/x"],
    }


def test_fast_on_50k_claims(db, owner, owner_url):
    seed_legacy(owner, per_layer=17_000)  # 51,000 unattributed claims
    seq = owner.execute("select max(seq) m from change").fetchone()["m"]
    took = migrate(owner_url)
    assert owner.execute("select count(*) n from claim where worker_id is null").fetchone()["n"] == 0
    assert owner.execute("select max(seq) m from change").fetchone()["m"] == seq
    print(f"0008 on 51k claims: {took:.2f}s")
    assert took < 30
