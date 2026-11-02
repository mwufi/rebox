"""Example connector: reads JSON files from ``fixtures/raw/example/`` (no network).

It exists to prove the pipeline and to be the template for real connectors. The fixtures
describe two companies and three people whose identity keys overlap across files, so one
sync exercises every resolver pass:

* ``01-acme.json`` mints Acme (by ``domain``), Ada (by ``github``) and Bo (no identity key).
* ``02-globex.json`` mints Globex and Cy.
* ``03-ada.json`` matches Ada by a differently written GitHub URL, and Acme by domain.
* ``04-bo.json`` has no identity key: Bo is matched by name within the context of Acme.
* ``05-cy.json`` matches Cy by X handle and adds an ``hn_user`` identity.

Fixture file shape (``payload`` of the RawRecord)::

    {"id": "company:acme", "kind": "company", "name": "...", "identity": {...},
     "attrs": {"description": "..."},
     "edges": [{"predicate": "founded", "dir": "in", "value": null,
                "entity": {"kind": "person", "name": "...", "identity": {...}}}],
     "context": [{"kind": "company", "identity": {...}}]}
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from ..models import ClaimInput, CursorUpdate, EntityRef, RawRecord
from .base import Ctx


class Example:
    name = "example"
    layer = "public/example"
    interval_s = 86400
    scheduled = False  # test data: runs only on `rebox-worker sync example`, never on the schedule

    # ── fetch: the only part that does I/O ──
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        """One RawRecord per fixture file, then a CursorUpdate (a full refresh every run;
        unchanged files are skipped by content hash)."""
        folder = ctx.fixtures_for(self.name)
        files = sorted(folder.glob("*.json"))
        if not files:
            raise FileNotFoundError(f"no example fixtures in {folder} (set FIXTURES_DIR?)")
        for n, path in enumerate(files, start=1):
            if ctx.limit is not None and n > ctx.limit:
                break
            payload = json.loads(path.read_text(encoding="utf-8"))
            yield RawRecord.new(self.name, payload["id"], payload, url=f"fixture:{self.name}/{path.name}")
            yield CursorUpdate(cursor={"last_file": path.name})

    # ── extract: pure ──
    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        p: dict[str, Any] = rec.payload
        ext = rec.external_id
        me = _ref(p, with_context=True)
        out: list[ClaimInput] = []

        def claim(predicate: str, *, subject: EntityRef = me, key: str = "", **kw: Any) -> None:
            ref = f"{self.name}:{ext}:{predicate}" + (f":{key}" if key else "")
            out.append(ClaimInput(subject=subject, predicate=predicate, source_ref=ref, **kw))

        if p.get("name"):
            claim("name", value=p["name"])
        for pred, value in sorted(p.get("identity", {}).items()):
            claim(pred, value=value)  # identities are facts too, so they are queryable
        for pred, value in sorted(p.get("attrs", {}).items()):
            claim(pred, value=value)
        for edge in p.get("edges", []):
            other = _ref(edge["entity"])
            key = _edge_key(edge["entity"])
            for pred, value in sorted(edge["entity"].get("identity", {}).items()):
                claim(pred, subject=other, key=key, value=value)  # the other end's identities too
            fields = {"value": edge.get("value"), "valid_from": edge.get("valid_from")}
            if edge.get("dir", "out") == "out":
                claim(edge["predicate"], object=other, key=key, **fields)
            else:  # edge points at this record's entity, e.g. "<person> founded <this company>"
                claim(edge["predicate"], subject=other, object=me, key=f"in:{key}", **fields)
        return out


def _ref(d: dict[str, Any], *, with_context: bool = False) -> EntityRef:
    context = [_ref(c) for c in d.get("context", [])] if with_context else []
    return EntityRef(
        kind=d["kind"], identity=d.get("identity", {}), name=d.get("name"), context=context or None
    )


def _edge_key(d: dict[str, Any]) -> str:
    """A stable, readable key for the other end of an edge (for source_ref)."""
    ident = d.get("identity") or {}
    if ident:
        k = sorted(ident)[0]
        return f"{k}={ident[k]}"
    return f"name={d.get('name', '')}"
