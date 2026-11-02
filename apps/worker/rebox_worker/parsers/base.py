"""``ParserConnector``: a connector whose fetch reads other sources' raw records and asks a model.

fetch
    1. Page through ``raw_record`` rows of the upstream sources in ``(fetched_at, source,
       external_id)`` order after the cursor. A changed upstream record is re-indexed with a new
       ``fetched_at``, so it comes round again (re-parse on ``content_hash`` change). Bumping
       ``PROMPT_VERSION`` or the model resets the cursor (deliberate re-parse).
    2. Read each record from the raw store, turn it into an ``InputDoc`` (``inputs.doc_for``),
       skip it unless ``accepts(doc)``, and skip it if this parser already stored an output for
       the same input hash, prompt version and model.
    3. Send ``batch_size`` docs per model call; look up in the graph (``ctx.db``) the identities
       and names the answers mention, so ``extract`` stays pure.
    4. Yield one RawRecord per input: ``{input_ref, input_hash, model, prompt_version, input,
       output, graph}`` (``external_id = input_ref``), then a CursorUpdate.
    Stops at ``ctx.limit`` inputs or ``PARSER_MAX_CALLS_PER_RUN`` model calls (whichever first);
    the cursor then points at the last parsed input, so the next run continues.

extract
    ``extract.extract_claims`` (pure). Replays read the stored outputs: no model calls.

Run stats: ``run_stats()`` returns this run's model usage (api_calls, input/output and cache
tokens, cost_usd, failed_inputs); the pipeline merges it into the run record (CONTRACTS §12). It
is also logged and saved in the cursor under ``last_run``.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from typing import Any

from ..models import ClaimInput, CursorUpdate, RawRecord, canonical_json
from ..normalize import normalize_name
from ..rawstore import RawStore
from .extract import extract_claims
from .inputs import doc_for
from .llm import KEY_ENV, AnthropicModel, Model, Usage, model_name
from .prompt import PROMPT_VERSION, InputDoc
from .schema import parse_facts

log = logging.getLogger(__name__)

MAX_CALLS_ENV = "PARSER_MAX_CALLS_PER_RUN"
DEFAULT_MAX_CALLS = 2000
BATCH_ENV = "PARSER_BATCH"
PAGE = 500

UPSTREAM_SQL = """
select source, external_id, store_key, content_hash, fetched_at
  from raw_record
 where source = any(%(sources)s)
   and (fetched_at, source, external_id) > (%(at)s::timestamptz, %(src)s, %(ext)s)
 order by fetched_at, source, external_id
 limit %(limit)s
"""

# identity predicate + normalized value -> kinds of the (unmerged) entities that carry it
IDENTITY_SQL = """
select c.predicate, c.value_text as value, array_agg(distinct e.kind) as kinds
  from unnest(%s::text[], %s::text[]) as k(p, v)
  join claim c on c.predicate = k.p and c.value_text = k.v and c.object_id is null and not c.retract
  join entity e on e.id = c.subject and e.merged_into is null
 group by c.predicate, c.value_text
"""

# exact (case-insensitive) public names of companies; trigram index on search_name serves ilike
NAME_SQL = """
select s.name, s.entity_id
  from search_name s join entity e on e.id = s.entity_id and e.merged_into is null
 where e.kind = 'company' and s.name ilike any(%s::text[])
"""


def input_hash(doc: InputDoc) -> str:
    return (
        "sha256:"
        + hashlib.sha256(canonical_json({"kind": doc.kind, "fields": doc.fields}).encode()).hexdigest()
    )


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ParserConnector:
    """Subclasses set ``name``, ``layer``, ``upstream``, ``worker_name``, ``batch_size`` and
    ``accepts``."""

    name = "parser"
    layer = "public/parser_ai"
    interval_s = 3600
    upstream: tuple[str, ...] = ()
    worker_name = "parser"  # CONTRACTS §12: worker id "<worker_name>-parser-<6 digits>"
    worker_kind = "parser"
    batch_size = 10

    def __init__(
        self,
        *,
        model: Model | None = None,
        raw: RawStore | None = None,
        model_factory: Callable[[Mapping[str, str]], Model] | None = None,
    ) -> None:
        self.model = model
        self.raw = raw
        self.model_factory = model_factory
        self.usage = Usage()

    def accepts(self, doc: InputDoc) -> bool:
        raise NotImplementedError

    def enabled(self, ctx: Any) -> bool:
        return self.model is not None or bool((ctx.env.get(KEY_ENV) or "").strip())

    def run_stats(self) -> dict[str, Any]:
        return self.usage.as_dict()

    # ── fetch ──

    def _model(self, env: Mapping[str, str]) -> Model:
        if self.model is not None:
            return self.model
        if self.model_factory is not None:
            self.model = self.model_factory(env)
            return self.model
        key = (env.get(KEY_ENV) or "").strip()
        if not key:
            raise RuntimeError(f"{self.name} parser is disabled: set {KEY_ENV}")
        self.model = AnthropicModel(key, name=model_name(env))
        return self.model

    def _raw(self, ctx: Any) -> RawStore:
        if self.raw is None:
            url = ctx.env.get("RAW_STORE") or os.environ.get("RAW_STORE") or "file://./.data/raw"
            self.raw = RawStore.from_url(url, ctx.db)
        return self.raw

    def fetch(self, ctx: Any, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        if ctx.db is None:
            raise RuntimeError(f"{self.name} needs ctx.db to read upstream raw records")
        model = self._model(ctx.env)
        raw = self._raw(ctx)
        self.usage = Usage()
        max_calls = _int(ctx.env.get(MAX_CALLS_ENV), DEFAULT_MAX_CALLS)
        batch_size = max(1, _int(ctx.env.get(BATCH_ENV), self.batch_size))
        version = {"prompt_version": PROMPT_VERSION, "model": model.name}
        cur = dict(cursor or {})
        if {k: cur.get(k) for k in version} != version:
            if cursor:
                ctx.log.info("%s: prompt/model changed (%s); re-parsing from the start", self.name, version)
            cur = {}
        pos = cur.get("after") or ["1970-01-01T00:00:00+00:00", "", ""]
        safe = pos  # everything up to here is parsed or skipped
        produced = 0
        pending: list[tuple[InputDoc, list[Any]]] = []

        def cursor_now() -> CursorUpdate:
            return CursorUpdate(cursor={**version, "after": safe, "last_run": self.usage.as_dict()})

        def flush() -> Iterator[RawRecord | CursorUpdate]:
            nonlocal produced, safe
            yield from self._flush(ctx, model, pending)
            produced += len(pending)
            safe = pending[-1][1]
            pending.clear()
            yield cursor_now()

        stop = False
        while not stop:
            with ctx.db.view() as conn:
                rows = conn.execute(
                    UPSTREAM_SQL,
                    {
                        "sources": list(self.upstream),
                        "at": pos[0],
                        "src": pos[1],
                        "ext": pos[2],
                        "limit": PAGE,
                    },
                ).fetchall()
            if not rows:
                break
            seen: list[tuple[InputDoc | None, list[Any]]] = []
            for row in rows:
                pos = [_iso(row["fetched_at"]), row["source"], row["external_id"]]
                try:
                    doc = doc_for(raw.get(row["store_key"]))
                except Exception:
                    ctx.log.exception("%s: unreadable upstream record %s", self.name, row["store_key"])
                    doc = None
                seen.append((doc if doc is not None and self.accepts(doc) else None, pos))
            done = self._already_parsed(ctx, raw, [d for d, _ in seen if d is not None], version)
            for doc, p in seen:
                if doc is None or doc.ref in done:
                    if not pending:
                        safe = p
                    continue
                pending.append((doc, p))
                full = len(pending) >= batch_size
                at_limit = ctx.limit is not None and produced + len(pending) >= ctx.limit
                if full or at_limit:
                    if self.usage.api_calls >= max_calls:
                        pending.clear()
                        stop = True
                        break
                    yield from flush()
                    if at_limit or self.usage.api_calls >= max_calls:
                        stop = True
                        break
            if not stop and not pending:
                yield cursor_now()
        if pending and self.usage.api_calls < max_calls:
            yield from flush()
        yield cursor_now()
        ctx.log.info("%s: parsed %d inputs; usage %s", self.name, produced, self.usage.as_dict())

    def _flush(self, ctx: Any, model: Model, batch: list[tuple[InputDoc, list[Any]]]) -> Iterator[RawRecord]:
        docs = [d for d, _ in batch]
        outputs = model.parse(docs, self.usage)
        graph = self._graph(ctx, docs, outputs)
        for doc, out in zip(docs, outputs, strict=True):
            payload = {
                "input_ref": doc.ref,
                "input_hash": input_hash(doc),
                "model": model.name,
                "prompt_version": PROMPT_VERSION,
                "input": {"kind": doc.kind, "fields": doc.fields, "meta": doc.meta},
                "output": out,
                "graph": {k: v for k, v in graph.items() if _mentions(doc, out, k)},
            }
            yield RawRecord.new(self.name, doc.ref, payload, url=str(doc.meta.get("url") or "") or None)

    def _already_parsed(
        self, ctx: Any, raw: RawStore, docs: list[InputDoc], version: dict[str, Any]
    ) -> set[str]:
        """Refs whose stored output was made from the same input, prompt version and model."""
        if not docs:
            return set()
        with ctx.db.view() as conn:
            rows = conn.execute(
                "select external_id, store_key from raw_record where source = %s and external_id = any(%s)",
                (self.name, [d.ref for d in docs]),
            ).fetchall()
        by_ref = {d.ref: d for d in docs}
        done: set[str] = set()
        for r in rows:
            try:
                p = raw.get(r["store_key"]).payload
            except Exception:
                continue
            if (
                p.get("input_hash") == input_hash(by_ref[r["external_id"]])
                and p.get("prompt_version") == version["prompt_version"]
                and p.get("model") == version["model"]
                and p.get("output") is not None
            ):
                done.add(r["external_id"])
        return done

    def _graph(self, ctx: Any, docs: list[InputDoc], outputs: list[dict[str, Any] | None]) -> dict[str, Any]:
        """What the graph knows about identities and names the inputs and answers mention."""
        pairs: set[tuple[str, str]] = set()
        names: set[str] = set()
        for doc, out in zip(docs, outputs, strict=True):
            m = doc.meta
            if m.get("x_id"):
                pairs.add(("x_id", str(m["x_id"])))
            if m.get("handle") and doc.kind != "github_bio":
                pairs.add(("x_handle", str(m["handle"]).lower()))
            if m.get("github"):
                pairs.add(("github", str(m["github"]).lower()))
            facts, _ = parse_facts((out or {}).get("facts"))
            platform = "github" if doc.kind == "github_bio" else "x_handle"
            for f in facts:
                d = f.model_dump()
                for key in ("org_handle", "acquirer_handle", "target_handle"):
                    if d.get(key):
                        pairs.add((platform, d[key].lstrip("@").lower()))
                if d.get("person_handle"):
                    pairs.add(("x_handle", d["person_handle"].lstrip("@").lower()))
                for key in ("org", "acquirer", "target"):
                    if d.get(key):
                        names.add(d[key])
                if d.get("type") == "location":
                    from .places import canonical_city

                    hit = canonical_city(d.get("place") or "")
                    if hit:
                        pairs.add(("wikidata", hit[1]))
        out: dict[str, Any] = {}
        with ctx.db.view() as conn:
            if pairs:
                ps, vs = zip(*sorted(pairs), strict=True)
                for r in conn.execute(IDENTITY_SQL, (list(ps), list(vs))):
                    out[f"{r['predicate']}:{r['value']}"] = sorted(r["kinds"])
            if names:
                rows = conn.execute(NAME_SQL, ([_like_escape(n) for n in sorted(names)],)).fetchall()
                ids: dict[str, set[str]] = {}
                for r in rows:
                    ids.setdefault(normalize_name(r["name"]), set()).add(r["entity_id"])
                for key, s in ids.items():
                    out[f"name:{key}"] = sorted(s)
        return out

    # ── extract ──

    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        if not isinstance(rec.payload, dict) or "input" not in rec.payload:
            return []
        return extract_claims(self.name, rec.payload)


def _mentions(doc: InputDoc, out: dict[str, Any] | None, key: str) -> bool:
    """Keep only the graph entries relevant to this input (small, stable raw records)."""
    pred, _, value = key.partition(":")
    m = doc.meta
    if pred == "x_id":
        return value == str(m.get("x_id"))
    if pred == "wikidata":
        return out is not None and any(
            f.get("type") == "location" for f in out.get("facts") or [] if isinstance(f, dict)
        )
    text = doc.text().casefold()
    if pred in ("x_handle", "github"):
        own = str(m.get("handle") or "").lower() == value or str(m.get("github") or "").lower() == value
        return own or f"@{value}" in text
    if pred == "name":
        return value in normalize_name(doc.text())
    return False


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, datetime) else str(v)


def _int(v: Any, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default
