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
    registrable_label,
    run_dedupe,
    safe_target,
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
            rid = body.get("review_id")
            if rid is not None:
                rv = conn.execute(
                    "select status, entity_ids from review_item where id = %s for update", (rid,)
                ).fetchone()
                if rv is None:
                    raise ValueError(f"review item {rid} not found")
                if rv[0] != "open":
                    raise ValueError(f"review item {rid} is already {rv[0]}")
                if src not in rv[1]:
                    raise ValueError(f"{src} is not a candidate of review item {rid}")
            conn.execute("update entity set merged_into = %s where id = %s", (dst, src))
            if rid is not None:
                conn.execute(
                    "update review_item set status = 'merged', resolved_by = 'admin', resolved_at = now()"
                    " where id = %s",
                    (rid,),
                )
            conn.execute(
                "insert into merge_log (op, from_id, into_id, actor) values ('merge', %s, %s, 'admin')",
                (src, dst),
            )
            seq = conn.execute(
                "insert into change (op, layer_id, subject, object_id)"
                " values ('merge', null, %s, %s) returning seq",
                (src, dst),
            ).fetchone()[0]
        return {"merged": {"from": src, "into": dst}, "review_id": rid, "seq": seq}

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
        def merge(self, f: str, i: str, review_id: int | None = None) -> None:
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


def test_safe_target_rules():
    assert registrable_label("google.com") == "google"
    assert registrable_label("app.notion.so") == "notion"
    assert registrable_label("acme.co.uk") == "acme"
    assert registrable_label("shop.acme.com.au") == "acme"
    assert registrable_label("co.uk") == "co"  # degenerate, never equal to a real name
    assert registrable_label("localhost") is None
    assert safe_target(_p("t", wikidata="Q1"), ["capital"], 0) == "wikidata"
    assert safe_target(_p("t", domain=["x.com", "ramp.com"]), ["ramp"], 0) == "domain ramp.com"
    assert safe_target(_p("t", domain="scale.com"), ["scale ai"], 0) is None
    assert safe_target(_p("t", domain="scaleai.com"), ["scale ai"], 0) == "domain scaleai.com"
    assert safe_target(_p("t", domain="acme.co.uk"), ["acme"], 0) == "domain acme.co.uk"
    assert safe_target(_p("t", x_handle="capital"), ["capital"], 4) is None
    assert safe_target(_p("t", x_handle="capital"), ["capital"], 5) == "5 referrers"


def test_name_only_safe_targets_and_short_names(db, client, merger, g, owner):
    # one keyed "Capital" (an X account: no wikidata, no capital.* domain, few referrers) -> review
    cap = g.entity("company", "cap", "Capital", layer="public/x_spider", x_handle="capital", x_id="9")
    cap_n = g.entity("company", "capn", "capital", layer="public/x_ai")
    # keyed by a matching domain -> merge
    ramp = g.entity("company", "ramp", "Ramp", layer="public/x_spider", domain="ramp.com", x_id="10")
    ramp_n = g.entity("company", "rampn", "Ramp", layer="public/x_ai")
    # keyed by an X handle only, but well referenced (5 people work there) -> merge
    acme = g.entity("company", "acme", "Acme Robotics", layer="public/x_spider", x_handle="acmerobo")
    for i in range(5):
        g.edge(
            g.entity("person", f"emp{i}", f"Emp {i}", layer="public/hn", hn_user=f"emp{i}"), "worked_at", acme
        )
    acme_n = g.entity("company", "acmen", "Acme Robotics, Inc.", layer="public/x_ai")
    # no keyed company: short names are left alone, distinctive ones converge
    m1 = g.entity("company", "meta1", "Meta", layer="public/x_ai")
    m2 = g.entity("company", "meta2", "Meta", layer="public/x_ai")
    lux1 = g.entity("company", "lux1", ["Lux Capital", "Lux Capital"], layer="public/x_ai")
    lux2 = g.entity("company", "lux2", "Lux Capital", layer="public/x_ai")

    rep = dedupe(db, client, merger)
    m = merged(owner)
    assert m == {ramp_n: ramp, acme_n: acme, lux2: lux1}
    assert cap_n not in m and m1 not in m and m2 not in m
    assert rep.stats["name_only_unsafe"] == 1 and rep.stats["name_only_skipped_short"] == 1
    assert rep.stats["name_only_merged"] == 3
    assert rep.skipped_short == [("meta", 2)]
    (rv,) = reviews(owner)
    assert rv["entity_ids"] == sorted([cap, cap_n]) and rv["reason"].startswith("name_only_unsafe_target")
    assert rv["evidence"][0]["safe"] is None and rv["evidence"][0]["referrers"] == 0
    reasons = {d.target.id: d.reason for d in rep.decisions if d.action == "merge"}
    assert "domain ramp.com" in reasons[ramp] and "5 referrers" in reasons[acme]
    # idempotent: nothing merged or reviewed again
    rep2 = dedupe(db, client, merger)
    assert rep2.stats["name_only_merged"] == 0 and rep2.stats["review_opened"] == 0
    assert rep2.stats["name_only_unsafe"] == 1 and rep2.stats["review_existing"] == 1


def test_name_only_merges_only_mention_only_companies(db, client, merger, g, owner):
    """S18: a name-only company with facts from a non-parser layer is a real, separately sourced
    company (HN's "Ditto", YC W20), not a bio mention: reviewed against a safe keyed target, never
    merged; left alone otherwise. Only mention-only companies (all claims in public/*_ai) merge."""
    ditto = g.entity("company", "dittoai", "Ditto", layer="public/exa", domain="ditto.ai")
    hn_ditto = g.entity("company", "dittohn", "Ditto", layer="public/hn", yc_batch="W20")
    launch = g.entity("launch", "dittolaunch", "Launch HN: Ditto (YC W20)", layer="public/hn")
    g.edge(hn_ditto, "launched", launch, layer="public/hn")
    bio_ditto = g.entity("company", "dittobio", "Ditto", layer="public/x_ai")
    homebase = g.entity(
        "company", "homebase", "Homebase", layer="public/exa", domain="joinhomebase.com", wikidata="Q777"
    )
    hn_homebase = g.entity("company", "homebasehn", "Homebase", layer="public/hn", yc_batch="W21")
    bio_homebase = g.entity("company", "homebasebio", "Homebase", layer="public/x_ai")
    # mixed: a parser name plus one structured fact -> structured too
    mixed = g.entity("company", "dittomix", "Ditto", layer="public/x_ai")
    g.client.write(
        "public/hn",
        [g._claim(mixed, "company", "yc_batch", "S19", "mix:yc")],
        worker=g.worker,
    )
    # no keyed match: mention-only ones converge among themselves, the HN one stays alone
    q1 = g.entity("company", "q1", "Quillworks Labs", layer="public/x_ai", created="2026-01-01")
    q2 = g.entity("company", "q2", "Quillworks Labs", layer="public/x_ai", created="2026-01-02")
    q_hn = g.entity("company", "qhn", "Quillworks Labs", layer="public/hn", yc_batch="S24")
    # unsafe keyed match: the mention-only one is reviewed (unsafe), the structured one left alone
    cap = g.entity("company", "cap", "Capital", layer="public/x_spider", x_handle="capital", x_id="9")
    cap_n = g.entity("company", "capn", "capital", layer="public/x_ai")
    cap_hn = g.entity("company", "caphn", "Capital", layer="public/hn", yc_batch="W22")

    dry = Dedupe(db, dry_run=True).run()
    assert dry.stats["name_only_structured"] == 3 and dry.stats["name_only_structured_left"] == 2
    rep = dedupe(db, client, merger)
    m = merged(owner)
    assert m == {bio_ditto: ditto, bio_homebase: homebase, q2: q1}
    assert rep.stats["name_only_merged"] == 3
    assert rep.stats["name_only_structured"] == 3
    assert rep.stats["name_only_structured_left"] == 2
    assert {i for i, _, _ in rep.structured_left} == {q_hn, cap_hn}
    rvs = {tuple(r["entity_ids"]): r for r in reviews(owner)}
    assert set(rvs) == {
        tuple(sorted([ditto, hn_ditto])),
        tuple(sorted([ditto, mixed])),
        tuple(sorted([homebase, hn_homebase])),
        tuple(sorted([cap, cap_n])),
    }
    hn = rvs[tuple(sorted([ditto, hn_ditto]))]
    assert hn["reason"].startswith("name_only_structured") and hn["kind"] == "possible_duplicate"
    assert hn["evidence"][0]["structured"] is True and hn["evidence"][0]["safe"] == "domain ditto.ai"
    assert rvs[tuple(sorted([homebase, hn_homebase]))]["evidence"][0]["safe"] == "wikidata"
    assert rvs[tuple(sorted([cap, cap_n]))]["reason"].startswith("name_only_unsafe_target")
    briefs = {b["id"]: b for d in rep.decisions for b in d.as_dict()["members"]}
    assert briefs[hn_ditto]["mention_only"] is False and briefs[cap_n]["mention_only"] is True
    # idempotent: nothing merged or reviewed again
    rep2 = dedupe(db, client, merger)
    assert rep2.stats["name_only_merged"] == 0 and rep2.stats["review_opened"] == 0
    assert rep2.stats["review_existing"] == 4


def test_merge_resolves_an_open_review_of_exactly_that_pair(db, client, merger, admin, g, owner):
    a = g.entity("company", "ra", "Res", wikidata="Q42", domain="res.com")
    b = g.entity("company", "rb", "Res", layer="public/exa", domain="res.com")
    c = g.entity("company", "rc", "Other", layer="public/exa", domain="other.com")
    pair = owner.execute(
        "insert into review_item (kind, entity_ids, reason) values ('ambiguous_match', %s, 'x') returning id",
        (sorted([a, b]),),
    ).fetchone()["id"]
    wider = owner.execute(
        "insert into review_item (kind, entity_ids, reason)"
        " values ('possible_duplicate', %s, 'y') returning id",
        (sorted([a, b, c]),),
    ).fetchone()["id"]
    dry = Dedupe(db, dry_run=True).run()
    assert dry.decisions[0].result == f"would_merge 1, resolves review {pair}"
    rep = dedupe(db, client, merger)
    assert admin.calls == [("/v0/admin/merge", {"from": b, "into": a, "review_id": pair})]
    assert rep.stats["reviews_resolved"] == 1 and merged(owner) == {b: a}
    status = {r["id"]: r["status"] for r in owner.execute("select id, status from review_item")}
    assert status == {pair: "merged", wider: "open"}


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
    oa1 = g.entity("company", "oa1", "OpenAI", wikidata="Q2", domain="openai.com")
    oa2 = g.entity("company", "oa2", "OpenAI", wikidata="Q3", domain="openai.com")
    sf = g.entity("place", "sf", "San Francisco")
    g.edge(oa1, "based_in", sf)  # phase 3 finds the OpenAI pair again: still one review item
    g.edge(oa2, "based_in", sf)
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
    assert rep.stats["review_opened"] == 1 and rep.stats["review_existing"] == 1
    # the real run does what the dry run said
    real = dedupe(db, client, merger)
    assert merged(owner) == {ex: wd, nf: wd}
    assert len(reviews(owner)) == 1
    for k in (
        "groups",
        "auto_merged",
        "review_opened",
        "review_existing",
        "skipped_conflict",
        "name_only_merged",
    ):
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
        amb = owner.execute(
            "insert into review_item (kind, entity_ids, reason)"
            " values ('ambiguous_match', %s, 'r') returning id",
            (sorted([ex, wd]),),
        ).fetchone()["id"]
        rep = run_dedupe(db, client=client, merger=merger)
        rv = owner.execute("select status, resolved_by from review_item where id = %s", (amb,)).fetchone()
        assert rv == {"status": "merged", "resolved_by": "admin"} and rep.stats["reviews_resolved"] == 1
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
