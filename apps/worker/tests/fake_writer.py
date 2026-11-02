"""An in-process stand-in for ``POST /internal/claims`` (CONTRACTS §5).

It validates the request shape strictly and writes through the *app role* with
``rebox_set_view`` (visible = all public layers, writable = the request layer), exactly as
the real API must, so the worker can switch to the real API without changes.

Faults can be injected for retry tests: ``writer.fail_next = [503, "drop", ...]``.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

ID_RE = re.compile(r"^(rbx|local):[a-z]+:[a-z0-9-]+$")
CLAIM_KEYS = {"subject", "predicate", "object", "value", "valid_from", "valid_to", "retract", "source_ref"}
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
        self.requests: list[dict[str, Any]] = []
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

            def do_POST(self) -> None:
                if self.path != "/internal/claims":
                    return self._send(404, {"error": "not_found"})
                if self.headers.get("Authorization") != f"Bearer {writer.token}":
                    return self._send(401, {"error": "unauthorized"})
                with writer.lock:
                    fault = writer.fail_next.pop(0) if writer.fail_next else None
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
                    writer.requests.append(body)
                try:
                    result = writer.write(body)
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

    # ── the writer ──
    def write(self, body: Any) -> dict[str, int]:
        if not isinstance(body, dict) or set(body) != {"layer", "claims"}:
            raise Rejected([{"index": -1, "message": "body must be exactly {layer, claims}"}])
        layer, claims = body["layer"], body["claims"]
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
                                          retract, source_ref)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict (layer_id, source_ref, subject, predicate, coalesce(object_id, ''))
                       do update set value = excluded.value, valid_from = excluded.valid_from,
                                     valid_to = excluded.valid_to, retract = excluded.retract,
                                     updated_at = now()""",
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
                    ),
                )
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
        if p["object_kind"]:
            if c.get("object") is None:
                return "edge needs object"
            if c.get("value") is not None and not isinstance(c["value"], dict):
                return "edge value must be an object or null"
        else:
            if c.get("object") is not None:
                return "attribute must not have object"
            if c.get("value") is None or isinstance(c["value"], dict):
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
