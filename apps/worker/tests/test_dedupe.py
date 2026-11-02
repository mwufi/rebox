"""Dedupe pass (S15): shared-key merges, conflicts -> review, canonical target, name-only company
convergence, name+neighbor review, dry run, idempotency, admin splits, the run record.

Merges go through ``POST /v0/admin/merge``. Most tests use ``FakeAdmin`` below, an in-process
copy of ``apps/api/src/routes/admin.ts`` (same checks, same ``merge_log`` / ``change`` rows,
written as ``rebox_app`` under the admin view). The last test runs the pass against the real API
from this checkout (skipped when ``apps/api/node_modules`` is missing).
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from rebox_worker import cli
from rebox_worker.client import ClaimsClient
from rebox_worker.dedupe import (
    AdminClient,
    Dedupe,
    MergeError,
    Profile,
    fold_name,
    run_dedupe,
    strong_conflicts,
    target_key,
)
from rebox_worker.models import ClaimWrite, IdRef, MintRef, MintSpec

REPO = Path(__file__).resolve().parents[3]
API_DIR = REPO / "apps" / "api"
ADMIN_TOKEN = "test-admin-token"
LAYERS = ["public/wikidata", "public/x_spider", "public/x_ai", "public/exa", "public/hn"]


# ── an in-process admin API (CONTRACTS §7, mirrors apps/api/src/routes/admin.ts) ──


class FakeAdmin:
    def __init__(self, app_url: str) -> None:
        self.app_url = app_url
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        admin = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                if self.headers.get("Authorization") != f"Bearer {ADMIN_TOKEN}":
                    return self._send(401, {"error": "unauthorized"})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                admin.calls.append((self.path, body))
                fn = {"/v0/admin/merge": admin.merge, "/v0/admin/split": admin.split}.get(self.path)
                if fn is None:
                    return self._send(404, {"error": "not_found"})
                try:
                    return self._send(200, fn(body))
                except ValueError as e:
                    return self._send(
                        400, {"error": "validation", "details": [{"index": -1, "message": str(e)}]}
                    )

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H

    def _tx(self):
        conn = psycopg.connect(self.app_url)
        public = [r[0] for r in conn.execute("select id from layer where scope = 'public'")]
        conn.execute("select rebox_set_view(%s::text[], %s::text[])", (public, public))
        return conn

    def merge(self, body: dict[str, Any]) -> dict[str, Any]:
        src, dst = body["from"], body["into"]
        if src == dst:
            raise ValueError("cannot merge an entity into itself")
        with self._tx() as conn:
            rows = {
                r[0]: r
                for r in conn.execute(
                    "select id, kind, merged_into from entity where id = any(%s) order by id for update",
                    ([src, dst],),
                )
            }
            if src not in rows or dst not in rows:
                raise ValueError("not found")
            if rows[src][2]:
                raise ValueError(f"{src} is already merged into {rows[src][2]}; split it first")
            if rows[dst][2]:
                raise ValueError(f"{dst} is itself merged into {rows[dst][2]}")
            if rows[src][1] != rows[dst][1]:
                raise ValueError("kind mismatch")
            conn.execute("update entity set merged_into = %s where id = %s", (dst, src))
            conn.execute(
                "insert into merge_log (op, from_id, into_id, actor) values ('merge', %s, %s, 'admin')",
                (src, dst),
            )
            seq = conn.execute(
                "insert into change (op, layer_id, subject, object_id)"
                " values ('merge', null, %s, %s) returning seq",
                (src, dst),
            ).fetchone()[0]
        return {"merged": {"from": src, "into": dst}, "review_id": None, "seq": seq}

    def split(self, body: dict[str, Any]) -> dict[str, Any]:
        eid = body["id"]
        with self._tx() as conn:
            row = conn.execute("select merged_into from entity where id = %s for update", (eid,)).fetchone()
            if row is None or row[0] is None:
                raise ValueError(f"{eid} is not merged")
            conn.execute("update entity set merged_into = null where id = %s", (eid,))
            conn.execute(
                "insert into merge_log (op, from_id, into_id, actor) values ('split', %s, %s, 'admin')",
                (eid, row[0]),
            )
            conn.execute(
                "insert into change (op, layer_id, subject, object_id) values ('split', null, %s, %s)",
                (eid, row[0]),
            )
        return {"split": {"id": eid, "from": row[0]}}


@pytest.fixture
def admin(app_url: str) -> Iterator[FakeAdmin]:
    a = FakeAdmin(app_url)
    yield a
    a.stop()


@pytest.fixture
def merger(admin: FakeAdmin) -> Iterator[AdminClient]:
    m = AdminClient(admin.url, ADMIN_TOKEN, backoff_s=0.01)
    yield m
    m.close()


# ── building a graph through the writer ──


class Graph:
    """Writes entities and facts through a writer client (fake or real) as one connector."""

    def __init__(self, client: ClaimsClient, owner: psycopg.Connection) -> None:
        self.client = client
        self.owner = owner
        self.worker = client.register_worker(name="seed", kind="connector", source="seed", layers=LAYERS)
        self.n = 0

    def entity(
        self,
        kind: str,
        key: str,
        name: str | list[str] | None,
        *,
        layer: str = "public/wikidata",
        created: str | None = None,
        **facts: Any,
    ) -> str:
        eid = f"rbx:{kind}:{key}"
        names = [name] if isinstance(name, str) else (name or [])
        claims = []
        for i, n in enumerate(names):
            claims.append(self._claim(eid, kind, "name", n, f"{eid}:name:{i}"))
        for pred, v in facts.items():
            for j, val in enumerate(v if isinstance(v, list) else [v]):
                claims.append(self._claim(eid, kind, pred, val, f"{eid}:{pred}:{j}"))
        if not claims:
            claims.append(self._claim(eid, kind, "name", key, f"{eid}:name"))
        self.client.write(layer, claims, worker=self.worker)
        if created:
            self.owner.execute("update entity set created_at = %s where id = %s", (created, eid))
        return eid

    def edge(self, subject: str, predicate: str, obj: str, *, layer: str = "public/wikidata") -> None:
        self.n += 1
        self.client.write(
            layer,
            [
                ClaimWrite(
                    subject=IdRef(id=subject),
                    predicate=predicate,
                    object=IdRef(id=obj),
                    source_ref=f"edge:{self.n}",
                )
            ],
            worker=self.worker,
        )

    def _claim(self, eid: str, kind: str, pred: str, value: Any, ref: str) -> ClaimWrite:
        return ClaimWrite(
            subject=MintRef(mint=MintSpec(id=eid, kind=kind)), predicate=pred, value=value, source_ref=ref
        )


@pytest.fixture
def g(db, client: ClaimsClient, owner) -> Graph:
    return Graph(client, owner)


def dedupe(db, client, merger, **kw) -> Any:
    return run_dedupe(db, client=client, merger=merger, **kw)


def merged(owner) -> dict[str, str]:
    return {
        r["id"]: r["merged_into"]
        for r in owner.execute("select id, merged_into from entity where merged_into is not null")
    }


def reviews(owner) -> list[dict[str, Any]]:
    return owner.execute(
        "select kind, entity_ids, reason, evidence, status from review_item order by id"
    ).fetchall()


# ── pure ──


def test_fold_name():
    assert fold_name("Anysphere, Inc.", "company") == "anysphere"
    assert fold_name("  Figma  Inc ", "company") == "figma"
    assert fold_name("GRAMMARLY, INC.", "company") == "grammarly"
    assert fold_name("Slack Technologies, LLC", "company") == "slack technologies"
    assert fold_name("Acme Corp. Ltd", "company") == "acme"
    assert fold_name("Notion (Notion Labs)", "company") == "notion"
    assert fold_name("Wells Fargo (1852–1998)", "company") == "wells fargo"
    assert fold_name("Co", "company") == "co"  # never folded to nothing
    assert fold_name("Sa Co", "company") == "sa"
    assert fold_name("Cisco", "company") == "cisco"  # a suffix only as a whole word
    assert fold_name("Jane Co", "person") == "jane co"  # legal forms are a company thing
    assert fold_name("Ada Lovelace (she/her)", "person") == "ada lovelace"
    assert fold_name("(stealth)", "company") == "(stealth)"


def _p(pid: str, *, claims: int = 1, created: int = 1, **keys: Any) -> Profile:
    return Profile(
        pid,
        "company",
        datetime(2026, 1, created, tzinfo=UTC),
        claims,
        {},
        {k: set(v if isinstance(v, list) else [v]) for k, v in keys.items()},
    )


def test_canonical_target_rules():
    a, b, c = (
        _p("a", claims=9, created=1),
        _p("b", claims=2, created=2, wikidata="Q1"),
        _p("c", claims=9, created=3),
    )
    assert min([a, b, c], key=target_key).id == "b"  # wikidata first
    assert min([a, c], key=target_key).id == "a"  # most claims, tie -> oldest
    d = _p("d", claims=10, created=5)
    assert min([a, c, d], key=target_key).id == "d"  # most claims beats age


def test_strong_conflicts_rules():
    # companies: different wikidata or x_id conflict; several domains / x_handles do not
    assert strong_conflicts("company", [_p("a", wikidata="Q1"), _p("b", wikidata="Q2")])
    assert strong_conflicts("company", [_p("a", x_id="1"), _p("b", x_id="2")])
    assert not strong_conflicts("company", [_p("a", domain="a.com"), _p("b", domain=["a.com", "b.com"])])
    assert not strong_conflicts("company", [_p("a", x_handle="x"), _p("b", x_handle="y")])
    assert not strong_conflicts("company", [_p("a", wikidata="Q1"), _p("b")])  # one side missing
    assert not strong_conflicts("company", [_p("a", github="a"), _p("b", github="b")])
    people = [_p("a", x_handle="ada"), _p("b", x_handle="ada2")]
    for p in people:
        p.kind = "person"
    assert [c["predicate"] for c in strong_conflicts("person", people)] == ["x_handle"]


# ── phase 1: shared identity keys ──


def test_auto_merge_on_shared_domain_through_admin_api(db, client, merger, admin, g, owner):
    exa = g.entity("company", "figmaexa", "Figma", layer="public/exa", domain="figma.com")
    wd = g.entity("company", "figmawd", "Figma, Inc.", wikidata="Q96186334", domain="figma.com")
    rep = dedupe(db, client, merger)
    assert merged(owner) == {exa: wd}  # the wikidata entity is the target
    assert rep.stats["groups"] == 1 and rep.stats["auto_merged"] == 1 and rep.stats["review_opened"] == 0
    assert admin.calls == [("/v0/admin/merge", {"from": exa, "into": wd})]
    # merge_log + change were written by the admin API
    log = owner.execute("select op, from_id, into_id, actor from merge_log").fetchall()
    assert log == [{"op": "merge", "from_id": exa, "into_id": wd, "actor": "admin"}]
    ch = owner.execute("select op, layer_id, subject, object_id from change where op = 'merge'").fetchall()
    assert ch == [{"op": "merge", "layer_id": None, "subject": exa, "object_id": wd}]
    # the run is recorded as the dedupe enricher
    run = owner.execute(
        "select r.kind, r.status, r.stats, w.id, w.kind as wkind"
        " from run r join worker w on w.id = r.worker_id where w.source = 'dedupe'"
    ).fetchall()
    assert len(run) == 1 and run[0]["kind"] == "enrich" and run[0]["status"] == "ok"
    assert run[0]["wkind"] == "enricher" and run[0]["id"].startswith("dedupe-enricher-")
    assert len(run[0]["id"]) == len("dedupe-enricher-") + 6
    assert {
        k: run[0]["stats"][k] for k in ("groups", "auto_merged", "review_opened", "skipped_conflict")
    } == {
        "groups": 1,
        "auto_merged": 1,
        "review_opened": 0,
        "skipped_conflict": 0,
    }
    assert "name_only_merged" in run[0]["stats"]
    assert rep.worker == run[0]["id"]

    # idempotent: a second run finds nothing to merge
    admin.calls.clear()
    rep2 = dedupe(db, client, merger)
    assert rep2.stats["groups"] == 0 and rep2.stats["auto_merged"] == 0 and admin.calls == []
    assert owner.execute("select count(*) n from merge_log").fetchone()["n"] == 1
    assert owner.execute("select count(*) n from run where status = 'ok'").fetchone()["n"] == 2
    # same worker identity on every run
    assert owner.execute("select count(*) n from worker where source = 'dedupe'").fetchone()["n"] == 1


def test_groups_sharing_members_are_unioned_and_merged_into_one(db, client, merger, g, owner):
    a = g.entity("company", "a", "Acme", wikidata="Q7", domain="acme.com")
    b = g.entity(
        "company", "b", "Acme Inc", layer="public/x_spider", domain="acme.com", x_handle="acme", x_id="77"
    )
    c = g.entity("company", "c", "acme", layer="public/x_ai", x_handle="acme")
    rep = dedupe(db, client, merger)
    assert merged(owner) == {b: a, c: a}
    assert rep.stats["groups"] == 1 and rep.stats["auto_merged"] == 2
    d = rep.decisions[0]
    assert d.phase == "key" and d.target.id == a
    assert {"predicate": "domain", "value": "acme.com"} in d.evidence[0]["shared"]
    assert {"predicate": "x_handle", "value": "acme"} in d.evidence[0]["shared"]


def test_conflicting_wikidata_opens_a_review_instead(db, client, merger, admin, g, owner):
    a = g.entity("company", "oa1", "OpenAI", wikidata="Q21708200", domain="openai.com")
    b = g.entity("company", "oa2", "OpenAI Global", wikidata="Q124605186", domain="openai.com")
    rep = dedupe(db, client, merger)
    assert merged(owner) == {} and admin.calls == []
    assert rep.stats["skipped_conflict"] == 1 and rep.stats["review_opened"] == 1
    (rv,) = reviews(owner)
    assert rv["kind"] == "possible_duplicate" and rv["status"] == "open"
    assert rv["entity_ids"] == sorted([a, b])
    assert "domain=openai.com" in rv["reason"] and "conflicting wikidata" in rv["reason"]
    conflict = next(e["conflict"] for e in rv["evidence"] if "conflict" in e)
    assert conflict == {"predicate": "wikidata", "values": {a: ["Q21708200"], b: ["Q124605186"]}}
    # not reopened by the next run
    rep2 = dedupe(db, client, merger)
    assert rep2.stats["review_opened"] == 0 and rep2.stats["review_existing"] == 1
    assert len(reviews(owner)) == 1


def test_person_x_handle_conflict_is_reviewed_company_handles_are_not(db, client, merger, g, owner):
    p1 = g.entity("person", "ada1", "Ada", layer="public/hn", github="ada", x_handle="ada")
    p2 = g.entity("person", "ada2", "Ada L", layer="public/x_spider", github="ada", x_handle="ada_l")
    c1 = g.entity("company", "acme1", "Acme", layer="public/x_spider", domain="acme.com", x_handle="acme")
    c2 = g.entity("company", "acme2", "Acme", layer="public/x_spider", domain="acme.com", x_handle="acmehq")
    rep = dedupe(db, client, merger)
    assert merged(owner) == {c2: c1}  # same claim count: the oldest wins
    (rv,) = reviews(owner)
    assert rv["entity_ids"] == sorted([p1, p2]) and "conflicting x_handle" in rv["reason"]
    assert rep.stats == {**rep.stats, "groups": 2, "auto_merged": 1, "skipped_conflict": 1}


def test_canonical_target_most_claims_then_oldest(db, client, merger, g, owner):
    old = g.entity("company", "old", "Zed", layer="public/exa", created="2026-01-01", domain="zed.dev")
    new = g.entity("company", "new", "Zed", layer="public/exa", created="2026-02-01", domain="zed.dev")
    big = g.entity(
        "company", "big", ["Zed", "Zed Industries"], layer="public/x_spider", created="2026-03-01",
        domain="zed.dev", website="https://zed.dev",
    )  # fmt: skip
    dedupe(db, client, merger)
    assert merged(owner) == {old: big, new: big}  # most claims wins over age

    # tie on claims -> the oldest
    t1 = g.entity("company", "t1", "Tie", layer="public/exa", created="2026-05-01", domain="tie.com")
    t2 = g.entity("company", "t2", "Tie", layer="public/exa", created="2026-04-01", domain="tie.com")
    dedupe(db, client, merger)
    assert merged(owner)[t1] == t2


def test_split_by_an_admin_sticks(db, client, merger, admin, g, owner):
    a = g.entity("company", "s1", "Split", wikidata="Q5", domain="split.com")
    b = g.entity("company", "s2", "Split", layer="public/exa", domain="split.com")
    dedupe(db, client, merger)
    assert merged(owner) == {b: a}
    httpx.post(
        f"{admin.url}/v0/admin/split", json={"id": b}, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
    )
    assert merged(owner) == {}
    rep = dedupe(db, client, merger)
    assert merged(owner) == {}  # not merged again
    (rv,) = reviews(owner)
    assert rv["entity_ids"] == sorted([a, b]) and "split by an admin" in rv["reason"]
    assert rep.stats["auto_merged"] == 0 and rep.stats["skipped_conflict"] == 1


def test_merge_errors_are_counted_and_the_run_continues(db, client, g, owner):
    a = g.entity("company", "e1", "Err", wikidata="Q9", domain="err.com")
    g.entity("company", "e2", "Err", layer="public/exa", domain="err.com")

    class Refusing:
        def merge(self, f: str, i: str) -> None:
            raise MergeError(f"merge {f} -> {i}: HTTP 400: nope")

    rep = dedupe(db, client, Refusing())
    assert rep.stats["merge_errors"] == 1 and rep.stats["auto_merged"] == 0
    assert merged(owner) == {} and rep.decisions[0].target.id == a
    run = owner.execute("select status, stats from run").fetchone()
    assert run["status"] == "ok" and run["stats"]["merge_errors"] == 1


def test_limit_caps_the_groups_acted_on(db, client, merger, g, owner):
    for i in range(3):
        g.entity("company", f"l{i}a", f"L{i}", wikidata=f"Q10{i}", domain=f"l{i}.com")
        g.entity("company", f"l{i}b", f"L{i}", layer="public/exa", domain=f"l{i}.com")
    rep = dedupe(db, client, merger, limit=2)
    assert rep.stats["auto_merged"] == 2 and rep.stats["deferred"] == 1 and len(merged(owner)) == 2
    rep = dedupe(db, client, merger, limit=2)
    assert rep.stats["auto_merged"] == 1 and len(merged(owner)) == 3


# ── phase 2: name-only companies ──


def test_name_only_company_convergence(db, client, merger, g, owner):
    google = g.entity("company", "google", "Google LLC", wikidata="Q95", domain="google.com")
    g1 = g.entity("company", "g1", "Google", layer="public/x_ai")
    g2 = g.entity("company", "g2", ["google", "Google"], layer="public/x_ai")
    # no keyed Palantir: the name-only ones converge on the one with most claims
    p1 = g.entity("company", "p1", "Palantir", layer="public/x_ai", created="2026-01-01")
    p2 = g.entity(
        "company", "p2", ["Palantir", "Palantir Technologies"], layer="public/x_ai", created="2026-02-01"
    )
    p3 = g.entity("company", "p3", "Palantir", layer="public/x_ai", created="2026-01-15")
    # two keyed Notions: review, no merge
    n1 = g.entity("company", "n1", "Notion", layer="public/x_spider", x_handle="notionhq", x_id="1")
    g.entity("company", "n2", "Notion Labs, Inc. (Notion)", wikidata="Q123", domain="notion.so")
    n3 = g.entity("company", "n3", "Notion", layer="public/x_ai")
    n2_name = fold_name("Notion Labs, Inc. (Notion)", "company")
    assert n2_name == "notion labs"  # so only n1 matches 'notion' ...
    g.entity(
        "company", "n2b", "notion", layer="public/wikidata", website="https://notion.com"
    )  # ... and this
    # a stray second name never chains: "Ramp" once named "Glass" is not merged into Glass
    ramp = g.entity("company", "ramp", ["Ramp", "Ramp", "Ramp", "Glass"], layer="public/x_ai")
    glass = g.entity("company", "glass", "Glass", layer="public/exa", domain="glass.com")
    # persons are never merged by name
    g.entity("person", "jd1", "Jane Doe", layer="public/x_ai")
    g.entity("person", "jd2", "Jane Doe", layer="public/x_ai")

    rep = dedupe(db, client, merger)
    m = merged(owner)
    assert m[g1] == google and m[g2] == google
    assert m[p1] == p2 and m[p3] == p2
    assert n3 not in m and n1 not in m and ramp not in m and glass not in m
    assert not any(k.startswith("rbx:person:") for k in m)
    assert rep.stats["name_only_merged"] == 4
    (rv,) = [r for r in reviews(owner) if n1 in r["entity_ids"]]
    assert rv["entity_ids"] == sorted([n1, "rbx:company:n2b"]) and "2 keyed companies" in rv["reason"]
    assert rv["evidence"][0]["name_only"] == [n3]
    # idempotent
    rep2 = dedupe(db, client, merger)
    assert rep2.stats["name_only_merged"] == 0 and rep2.stats["review_opened"] == 0


# ── phase 3: name + neighbor ──


def test_name_and_shared_neighbor_is_reviewed_never_merged(db, client, merger, g, owner):
    trulia = g.entity("company", "trulia", "Trulia", domain="trulia.com")
    sf = g.entity("place", "sf", "San Francisco")
    pete1 = g.entity("person", "pete1", "Pete Flint", layer="public/x_spider", x_id="1", x_handle="pete")
    pete2 = g.entity("person", "pete2", "Pete Flint (Trulia)", layer="public/hn", hn_user="pflint")
    g.edge(pete1, "founded", trulia)
    g.edge(pete2, "worked_at", trulia)
    # same name, but only a place in common (a hub for people) or only a follows edge: no review
    d1 = g.entity("person", "d1", "David", layer="public/x_spider", x_id="2", x_handle="d1")
    d2 = g.entity("person", "d2", "David", layer="public/x_spider", x_id="3", x_handle="d2")
    g.edge(d1, "based_in", sf)
    g.edge(d2, "based_in", sf)
    g.edge(d1, "follows", trulia, layer="public/x_spider")
    g.edge(d2, "follows", trulia, layer="public/x_spider")
    # same-named companies in the same city are worth a look
    o1 = g.entity("company", "o1", "OpenAI", wikidata="Q21708200", domain="openai.com")
    o2 = g.entity("company", "o2", "OpenAI", wikidata="Q124605186")
    g.edge(o1, "based_in", sf)
    g.edge(o2, "based_in", sf)

    rep = dedupe(db, client, merger)
    assert merged(owner) == {}
    rvs = {tuple(r["entity_ids"]): r for r in reviews(owner)}
    assert set(rvs) == {tuple(sorted([pete1, pete2])), tuple(sorted([o1, o2]))}
    pete = rvs[tuple(sorted([pete1, pete2]))]
    assert pete["reason"].startswith("name+neighbor") and pete["evidence"][0]["shared_neighbors"] == [trulia]
    oa = rvs[tuple(sorted([o1, o2]))]
    assert "conflicting wikidata" in oa["reason"]
    assert rep.stats["name_neighbor_groups"] == 2 and rep.stats["review_opened"] == 2
    assert dedupe(db, client, merger).stats["review_opened"] == 0  # not reopened


def test_dismissed_review_is_not_reopened(db, client, merger, g, owner):
    c = g.entity("company", "co", "Co", domain="co.com")
    a = g.entity("person", "a1", "Ann Lee", layer="public/hn", hn_user="ann")
    b = g.entity("person", "a2", "Ann Lee", layer="public/x_spider", x_id="5", x_handle="annlee")
    g.edge(a, "worked_at", c)
    g.edge(b, "worked_at", c)
    dedupe(db, client, merger)
    owner.execute("update review_item set status = 'dismissed'")
    rep = dedupe(db, client, merger)
    assert rep.stats["review_opened"] == 0 and len(reviews(owner)) == 1


# ── dry run ──


def _counts(owner) -> dict[str, int]:
    return {
        t: owner.execute(f"select count(*) n from {t}").fetchone()["n"]
        for t in ("merge_log", "change", "review_item", "run", "worker", "claim")
    } | {
        "merged": owner.execute("select count(*) n from entity where merged_into is not null").fetchone()["n"]
    }


def test_dry_run_writes_nothing_and_plans_the_same(db, client, merger, admin, g, owner):
    wd = g.entity("company", "fwd", "Figma, Inc.", wikidata="Q1", domain="figma.com")
    ex = g.entity("company", "fex", "Figma", layer="public/exa", domain="figma.com")
    g.entity("company", "oa1", "OpenAI", wikidata="Q2", domain="openai.com")
    g.entity("company", "oa2", "OpenAI", wikidata="Q3", domain="openai.com")
    nf = g.entity("company", "fname", "Figma", layer="public/x_ai")  # name-only: joins the merged Figma
    before = _counts(owner)
    out = io.StringIO()
    rep = Dedupe(db, dry_run=True, out=out).run()
    assert _counts(owner) == before and admin.calls == []
    text = out.getvalue()
    assert f"merge into {wd}" in text and "would_merge 1" in text and "would_open_review" in text
    plan = {(d.phase, d.action, d.target.id if d.target else None) for d in rep.decisions}
    assert ("key", "merge", wd) in plan and ("name_only", "merge", wd) in plan
    assert rep.groups_by_predicate["domain"]["company"] == 2
    # the real run does what the dry run said
    real = dedupe(db, client, merger)
    assert merged(owner) == {ex: wd, nf: wd}
    for k in ("groups", "auto_merged", "review_opened", "skipped_conflict", "name_only_merged"):
        assert real.stats[k] == rep.stats[k], k


def test_cli_dry_run_and_config_errors(db, g, owner, app_url, monkeypatch, capsys):
    g.entity("company", "c1", "Cli", wikidata="Q1", domain="cli.com")
    g.entity("company", "c2", "Cli", layer="public/exa", domain="cli.com")
    monkeypatch.setenv("DATABASE_URL", app_url)
    for var in ("API_URL", "INGEST_TOKEN", "ADMIN_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["dedupe", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "domain    company 1, person 0" in out and '"auto_merged": 1' in out
    assert cli.main(["dedupe", "--dry-run", "--json", "--kinds", "person"]) == 0
    data = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert data["kinds"] == ["person"] and data["decisions"] == []
    assert merged(owner) == {}
    # a real run needs the writer and the admin token
    assert cli.main(["dedupe"]) == 2
    monkeypatch.setenv("API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("INGEST_TOKEN", "x")
    assert cli.main(["dedupe"]) == 2
    assert "ADMIN_TOKEN" in capsys.readouterr().err


# ── against the real admin API ──


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def real_api(app_url: str) -> Iterator[str]:
    tsx = API_DIR / "node_modules" / ".bin" / "tsx"
    if not tsx.exists():
        pytest.skip("apps/api/node_modules not installed (pnpm install)")
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": app_url,
        "PORT": str(port),
        "INGEST_TOKEN": "test-ingest-token",
        "ADMIN_TOKEN": ADMIN_TOKEN,
        "REBOX_ENV": "dev",
    }
    proc = subprocess.Popen(
        [str(tsx), "src/server.ts"], cwd=API_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(
                    f"api did not start: {proc.stderr.read().decode()[-2000:] if proc.stderr else ''}"
                )
            time.sleep(0.3)
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_dedupe_against_the_real_api(real_api, db, owner):
    client = ClaimsClient(real_api, "test-ingest-token", backoff_s=0.01, max_backoff_s=0.05)
    merger = AdminClient(real_api, ADMIN_TOKEN, backoff_s=0.01)
    try:
        g = Graph(client, owner)
        wd = g.entity("company", "rwd", "Figma, Inc.", wikidata="Q96186334", domain="figma.com")
        ex = g.entity(
            "company", "rex", "Figma", layer="public/exa", domain="figma.com", website="https://figma.com"
        )
        nf = g.entity("company", "rnf", "Figma", layer="public/x_ai")
        rep = run_dedupe(db, client=client, merger=merger)
        assert merged(owner) == {ex: wd, nf: wd}
        assert rep.stats["auto_merged"] == 1 and rep.stats["name_only_merged"] == 1
        log = owner.execute("select op, from_id, into_id, actor from merge_log order by id").fetchall()
        assert [(r["op"], r["from_id"], r["into_id"], r["actor"]) for r in log] == [
            ("merge", ex, wd, "admin"),
            ("merge", nf, wd, "admin"),
        ]
        ch = owner.execute("select subject, object_id from change where op = 'merge' order by seq").fetchall()
        assert [(r["subject"], r["object_id"]) for r in ch] == [(ex, wd), (nf, wd)]
        # reads follow the merge
        view = httpx.get(f"{real_api}/v0/entities/{ex}").json()
        assert view["redirect"] == wd
        run = owner.execute(
            "select kind, status, stats from run r where worker_id like 'dedupe-enricher-%'"
        ).fetchone()
        assert run["kind"] == "enrich" and run["status"] == "ok" and run["stats"]["auto_merged"] == 1
        # undo with the real split; the next pass leaves it split and asks for review instead
        r = httpx.post(
            f"{real_api}/v0/admin/split", json={"id": ex}, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        assert r.status_code == 200
        rep2 = run_dedupe(db, client=client, merger=merger)
        assert ex not in merged(owner) and rep2.stats["auto_merged"] == 0 and rep2.stats["review_opened"] == 1
        # a refused merge (already merged) is a MergeError, not a crash
        with pytest.raises(MergeError, match="already merged"):
            merger.merge(nf, wd)
    finally:
        client.close()
        merger.close()
