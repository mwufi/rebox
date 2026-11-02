"""An in-process stand-in for the writer API (CONTRACTS §5, §12).

``POST /internal/claims`` (with ``worker``, ``run_id``, per-claim ``evidence``),
``POST /internal/workers`` (idempotent on name+kind+source), ``POST /internal/runs``,
``PATCH /internal/runs/:id``.

It validates the request shape strictly and writes through the *app role* with
``rebox_set_view`` (visible = all public layers, writable = the request layer), exactly as
the real API must, so the worker can switch to the real API without changes.

Faults can be injected into claims writes for retry tests: ``writer.fail_next = [503, "drop", ...]``.
"""

from __future__ import annotations

import json
import random
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ID_RE = re.compile(r"^(rbx|local):[a-z]+:[a-z0-9-]+$")
CLAIM_KEYS = {
    "subject",
    "predicate",
    "object",
    "value",
    "valid_from",
    "valid_to",
    "retract",
    "source_ref",
    "evidence",
}
EVIDENCE_REQUIRED = {"parser", "enricher", "external"}
TRUST = {"connector": 0.8, "spider": 0.8, "parser": 0.6, "enricher": 0.5}
MAX_CLAIMS = 1000


class Rejected(Exception):
    def __init__(self, details: list[dict[str, Any]]):
        self.details = details


def _norm(meta: dict[str, Any]):
    # The fake reuses the worker's normalizers to check "identity values are normalized".
    from rebox_worker.normalize import get_normalizer

    return get_normalizer(meta.get("normalize"))


class FakeWriter:
    def __init__(self, app_url: str, token: str = "test-ingest-token") -> None:
        self.app_url = app_url
        self.token = token
        self.requests: list[dict[str, Any]] = []  # claims bodies
        self.calls: list[tuple[str, str, Any]] = []  # every request (method, path, body)
        self.fail_next: list[int | str] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeWriter:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ── HTTP ──
    def _handler(self):
        writer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_PATCH(self) -> None:
                m = re.fullmatch(r"/internal/runs/(\d+)", self.path)
                if not m:
                    return self._send(404, {"error": "not_found"})
                return self._dispatch(lambda body: writer.patch_run(int(m.group(1)), body))

            def do_POST(self) -> None:
                routes = {
                    "/internal/claims": writer.write,
                    "/internal/workers": writer.register,
                    "/internal/runs": writer.open_run,
                }
                if self.path not in routes:
                    return self._send(404, {"error": "not_found"})
                return self._dispatch(routes[self.path], claims=self.path == "/internal/claims")

            def _dispatch(self, fn, claims: bool = False) -> None:
                if self.headers.get("Authorization") != f"Bearer {writer.token}":
                    return self._send(401, {"error": "unauthorized"})
                with writer.lock:
                    # Faults target claims writes (what retry tests exercise), not registration/runs.
                    fault = writer.fail_next.pop(0) if claims and writer.fail_next else None
                if fault == "drop":
                    self.close_connection = True
                    self.connection.close()
                    return None
                if isinstance(fault, int):
                    return self._send(fault, {"error": "injected"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                except ValueError:
                    return self._send(
                        400, {"error": "validation", "details": [{"index": -1, "message": "bad json"}]}
                    )
                with writer.lock:
                    if claims:
                        writer.requests.append(body)
                    writer.calls.append((self.command, self.path, body))
                try:
                    result = fn(body)
                except Rejected as e:
                    return self._send(400, {"error": "validation", "details": e.details})
                return self._send(200, result)

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler

    # ── workers and runs ──
    def register(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not {"name", "kind", "source", "layers"} <= set(body):
            raise Rejected([{"index": -1, "message": "name, kind, source, layers required"}])
        if set(body) - {"name", "kind", "source", "layers", "config", "description", "id"}:
            raise Rejected([{"index": -1, "message": "unknown keys"}])
        name, kind, source = body["name"], body["kind"], body["source"]
        if kind not in TRUST or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
            raise Rejected([{"index": -1, "message": f"bad name/kind {name!r} {kind!r}"}])
        with psycopg.connect(self.app_url) as conn, conn.transaction():
            row = conn.execute(
                """select id, layers from worker where kind = %s and source = %s and owner = 'rebox'
                     and id ~ %s order by created_at, id limit 1""",
                (kind, source, f"^{name}-{kind}-[0-9]{{6}}$"),
            ).fetchone()
            if row:
                extra = [x for x in body["layers"] if x not in row[1]]
                if extra:
                    conn.execute("update worker set layers = layers || %s where id = %s", (extra, row[0]))
                return {"id": row[0], "created": False}
            wid = f"{name}-{kind}-{random.randint(0, 999999):06d}"
            conn.execute(
                """insert into worker (id, kind, source, trust, layers, config, description)
                   values (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    wid,
                    kind,
                    source,
                    TRUST[kind],
                    body["layers"],
                    Jsonb(body.get("config") or {}),
                    body.get("description"),
                ),
            )
        return {"id": wid, "created": True}

    def open_run(self, body: Any) -> dict[str, Any]:
        with psycopg.connect(self.app_url) as conn, conn.transaction():
            w = conn.execute("select source from worker where id = %s", (body.get("worker"),)).fetchone()
            if w is None:
                raise Rejected([{"index": -1, "message": "no such worker"}])
            rid = conn.execute(
                "insert into run (worker_id, source, kind) values (%s, %s, %s) returning id",
                (body["worker"], w[0], body["kind"]),
            ).fetchone()[0]
        return {"run_id": rid}

    def patch_run(self, run_id: int, body: Any) -> dict[str, Any]:
        if set(body) - {"status", "stats", "error"}:
            raise Rejected([{"index": -1, "message": "unknown keys"}])
        status = body.get("status")
        with psycopg.connect(self.app_url) as conn, conn.transaction():
            row = conn.execute(
                """update run set stats = stats || %s, status = coalesce(%s, status),
                          error = coalesce(%s, error),
                          finished_at = case when %s in ('ok', 'failed') then now() else finished_at end
                    where id = %s returning id, status""",
                (Jsonb(body.get("stats") or {}), status, body.get("error"), status, run_id),
            ).fetchone()
            if row is None:
                raise Rejected([{"index": -1, "message": "no such run"}])
        return {"id": row[0], "status": row[1]}

    # ── the writer ──
    def write(self, body: Any) -> dict[str, int]:
        if not isinstance(body, dict) or not {"layer", "worker", "claims"} <= set(body) <= {
            "layer",
            "worker",
            "run_id",
            "claims",
        }:
            raise Rejected([{"index": -1, "message": "body must be {layer, worker, run_id?, claims}"}])
        layer, claims, worker_id, run_id = body["layer"], body["claims"], body["worker"], body.get("run_id")
        if not isinstance(layer, str) or not isinstance(claims, list) or not claims:
            raise Rejected([{"index": -1, "message": "layer must be a string, claims a non-empty list"}])
        if len(claims) > MAX_CLAIMS:
            raise Rejected([{"index": -1, "message": f"at most {MAX_CLAIMS} claims"}])

        with psycopg.connect(self.app_url) as conn, conn.transaction():
            m = re.fullmatch(r"public/([a-z0-9_-]+)", layer)
            if m:
                # Layer auto-create needs writes to `layer` (no RLS on it).
                conn.execute(
                    "insert into layer (id, scope, source) values (%s, 'public', %s) on conflict do nothing",
                    (layer, m.group(1)),
                )
            public = [r[0] for r in conn.execute("select id from layer where scope = 'public'")]
            if layer not in public:
                raise Rejected([{"index": -1, "message": f"unknown or non-public layer {layer!r}"}])
            w = conn.execute("select kind, layers, status from worker where id = %s", (worker_id,)).fetchone()
            if w is None or w[2] != "active" or layer not in w[1]:
                raise Rejected([{"index": -1, "message": f"worker {worker_id!r} may not write {layer}"}])
            if run_id is not None:
                r = conn.execute("select worker_id from run where id = %s", (run_id,)).fetchone()
                if r is None or r[0] != worker_id:
                    raise Rejected([{"index": -1, "message": f"run {run_id} is not {worker_id}'s"}])
            if w[0] in EVIDENCE_REQUIRED:
                missing = [
                    {"index": i, "message": "evidence required"}
                    for i, c in enumerate(claims)
                    if isinstance(c, dict) and not c.get("evidence")
                ]
                if missing:
                    raise Rejected(missing)
            conn.execute("select rebox_set_view(%s::text[], %s::text[])", (public, [layer]))
            preds = {
                r[0]: {"subject_kinds": r[1], "object_kind": r[2], "meta": r[3] or {}}
                for r in conn.execute("select id, subject_kinds, object_kind, meta from predicate")
            }

            details: list[dict[str, Any]] = []
            mints: dict[str, str] = {}
            for i, c in enumerate(claims):
                err = self._validate(c, preds, layer, mints)
                if err:
                    details.append({"index": i, "message": err})
            if details:
                raise Rejected(details)

            # Mint (reuse when the id exists).
            minted = 0
            existing = {
                r[0]: r[1]
                for r in conn.execute("select id, kind from entity where id = any(%s)", (list(mints),))
            }
            for eid, kind in mints.items():
                if eid in existing:
                    continue
                conn.execute(
                    "insert into entity (id, kind, home_layer) values (%s, %s, %s)", (eid, kind, layer)
                )
                minted += 1

            # Referenced ids must exist and be visible.
            ref_ids = {_id(c["subject"]) for c in claims} | {
                _id(c["object"]) for c in claims if c.get("object")
            }
            kinds = {
                r[0]: r[1]
                for r in conn.execute("select id, kind from entity where id = any(%s)", (list(ref_ids),))
            }
            for i, c in enumerate(claims):
                for role in ("subject", "object"):
                    ref = c.get(role)
                    if ref and _id(ref) not in kinds:
                        details.append({"index": i, "message": f"{role} {_id(ref)} does not exist"})
                        continue
                    if not ref:
                        continue
                    p = preds[c["predicate"]]
                    k = kinds[_id(ref)]
                    if role == "subject" and k not in p["subject_kinds"]:
                        details.append({"index": i, "message": f"subject kind {k} not allowed"})
                    if role == "object" and p["object_kind"] != "*" and k != p["object_kind"]:
                        details.append({"index": i, "message": f"object kind {k} != {p['object_kind']}"})
            if details:
                raise Rejected(details)

            for c in claims:
                conn.execute(
                    """insert into claim (layer_id, subject, predicate, object_id, value,
                                          valid_from, valid_to,
                                          retract, source_ref, worker_id, run_id, evidence)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict (layer_id, source_ref, subject, predicate, coalesce(object_id, ''))
                       do update set value = excluded.value, valid_from = excluded.valid_from,
                                     valid_to = excluded.valid_to, retract = excluded.retract,
                                     evidence = excluded.evidence, worker_id = excluded.worker_id,
                                     run_id = excluded.run_id, updated_at = now()""",
                    (
                        layer,
                        _id(c["subject"]),
                        c["predicate"],
                        _id(c["object"]) if c.get("object") else None,
                        Jsonb(c["value"]) if c.get("value") is not None else None,
                        c.get("valid_from"),
                        c.get("valid_to"),
                        bool(c.get("retract", False)),
                        c["source_ref"],
                        worker_id,
                        run_id,
                        Jsonb(c["evidence"]) if c.get("evidence") else None,
                    ),
                )
            conn.execute("update worker set last_seen_at = now() where id = %s", (worker_id,))
        return {"written": len(claims), "minted": minted}

    @staticmethod
    def _validate(c: Any, preds: dict[str, Any], layer: str, mints: dict[str, str]) -> str | None:
        if not isinstance(c, dict):
            return "claim must be an object"
        extra = set(c) - CLAIM_KEYS
        if extra:
            return f"unknown keys {sorted(extra)}"
        if not isinstance(c.get("source_ref"), str) or not c["source_ref"]:
            return "source_ref required"
        p = preds.get(c.get("predicate"))
        if p is None:
            return f"unknown predicate {c.get('predicate')!r}"
        for role in ("subject", "object"):
            ref = c.get(role)
            if ref is None:
                if role == "subject":
                    return "subject required"
                continue
            if not isinstance(ref, dict) or len(ref) != 1:
                return f"{role} must be {{id}} or {{mint}}"
            if "id" in ref:
                if not isinstance(ref["id"], str) or not ID_RE.match(ref["id"]):
                    return f"{role}.id malformed"
            elif "mint" in ref:
                mt = ref["mint"]
                if not isinstance(mt, dict) or set(mt) != {"id", "kind"} or not ID_RE.match(str(mt["id"])):
                    return f"{role}.mint must be {{id, kind}} with a valid id"
                if mt["id"].split(":")[1] != mt["kind"]:
                    return f"{role}.mint kind does not match id"
                mints[mt["id"]] = mt["kind"]
            else:
                return f"{role} must be {{id}} or {{mint}}"
        if p["meta"].get("private") and layer.startswith("public/"):
            return f"private predicate {c['predicate']} in public layer"
        ev = c.get("evidence")
        if ev is not None and (
            not isinstance(ev, list)
            or not all(isinstance(e, dict) and str(e.get("url", "")).startswith("http") for e in ev)
        ):
            return "evidence must be a list of {url, author?, quote?, observed_at?}"
        if p["object_kind"]:
            if c.get("object") is None:
                return "edge needs object"
            if c.get("value") is not None and not isinstance(c["value"], dict):
                return "edge value must be an object or null"
        else:
            if c.get("object") is not None:
                return "attribute must not have object"
            if p["meta"].get("value_type") == "json":
                if not isinstance(c.get("value"), dict):
                    return "json attribute value must be an object"
            elif c.get("value") is None or isinstance(c["value"], dict):
                return "attribute value must be a scalar or array"
            if p["meta"].get("identity"):
                try:
                    if _norm(p["meta"])(c["value"]) != c["value"]:
                        return f"identity value for {c['predicate']} not normalized"
                except ValueError as e:
                    return f"identity value invalid: {e}"
        if "retract" in c and not isinstance(c["retract"], bool):
            return "retract must be bool"
        return None


def _id(ref: dict[str, Any]) -> str:
    return ref["id"] if "id" in ref else ref["mint"]["id"]
