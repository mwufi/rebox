"""Entity resolution: ``ClaimInput`` (descriptions of entities) -> ``ClaimWrite`` (entity ids).

For every entity described by an ``EntityRef`` the resolver decides, in order (CONTRACTS §8):

  1. **Hard keys.** A normalized identity value (``domain``, ``github``, ``wikidata``, ...)
     equals an existing identity claim -> that entity (following ``merged_into``).
     Several distinct entities -> open an ``ambiguous_match`` review item, use the oldest.
  2. **Scoped fuzzy.** The ref's ``context`` entities resolved, and a same-kind entity with
     an equal normalized name has an edge to one of them -> match (confidence 0.8).
  3. **Company name** (``company`` refs with no identity key only; persons never). Un-merged
     companies with a visible ``name`` that folds (``names.fold_name``) equal to the ref's.
     A keyed one (with an identity claim) is a target only if *safe*
     (``names.is_safe_name_target``: a wikidata claim, a domain whose label is the name, or
     5+ distinct subjects with edges to it). Exactly one keyed and safe -> that one (0.7);
     several keyed with a safe one -> the oldest safe one, plus a ``possible_duplicate``
     review item (once per entity set). Otherwise, if the name may converge
     (``names.converges_by_name``: 2+ tokens or 6+ chars) -> the name-only company with the
     most claims, tie oldest (0.6). Placeholder names ("stealth", "self-employed") skip
     this pass.
  4. **Mint.** A deterministic id (``ids.mint_id``). A company whose name may converge is
     minted from its folded name (``{"name": folded}``), so every later mention converges on
     it; other identity-less refs (short company names too) are scoped to their record. If
     the id already exists (an earlier run minted it), it is a match, not a new entity.

Every decision becomes a ``resolution_log`` row. A batch-local cache makes one batch resolve
consistently: the same identity value, the same identity-less (kind, name, context), or the
same folded company name (pass 3/4), always gets the same id, even before the writer has
stored it.

Claims are pre-checked against the vocabulary with the writer's rules (CONTRACTS §5) so one
bad claim is reported and dropped instead of failing the whole write batch with a 400.

Usage::

    resolver = Resolver(db, source="hn", layer="public/hn")
    res = resolver.resolve([(record.external_id, claim) for claim in claims])
    client.write(layer, res.claims)
    resolver.commit(res)            # resolution_log + review_item, only after a successful write
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .db import Db
from .ids import NAME_KEY, mint_id
from .models import (
    ClaimInput,
    ClaimWrite,
    Decision,
    EntityRef,
    Evidence,
    IdRef,
    MintRef,
    MintSpec,
    WriteRef,
)
from .names import converges_by_name, fold_name, is_placeholder_company, is_safe_name_target
from .normalize import normalize_name
from .predicates import Predicate, load_predicates

log = logging.getLogger(__name__)

# Pass 1 lookup: every (identity predicate, normalized value) of a batch in one query.
#
# Under RLS, Postgres evaluates only leakproof conditions before the policy filter
# (`layer_id = any(rebox_visible())`), so an index condition must use a leakproof operator.
# `value #>> '{}'` is not leakproof; `claim.value_text` (a stored generated column, migration
# 0004) compared with text `=` is. So each (predicate, value) pair is a true index lookup on
# claim_value_text_idx ((predicate, value_text) where object_id is null), as rebox_app too
# (CONTRACTS §2a). The per-predicate lateral keeps one index probe per predicate with
# `value_text = any(values)`, so the plan does not depend on the unnest row estimate.
HARD_KEY_SQL = """
select k.p as predicate, c.value_text as value, c.subject
  from (select p, array_agg(v) as vs
          from unnest(%s::text[], %s::text[]) as u(p, v)
         group by p) as k
  cross join lateral (
        select distinct c.value_text, c.subject
          from claim c
         where c.predicate = k.p and c.value_text = any (k.vs)
           and c.object_id is null and not c.retract
  ) as c
"""

# Pass 3 candidates: companies with a public name that is word-similar (pg_trgm ``%>``, GIN
# index on search_name) to a folded name of the batch. search_name has no RLS (it only holds
# names from public layers, CONTRACTS §2a); ``claim`` does, so ``name % $1`` there could not use
# an index as rebox_app. Folded equality is decided in Python against every visible name claim
# of the candidates (search_name keeps one name per entity and layer).
#
# One UNION ALL branch per name, each with its own bound value: the GIN index is only usable
# when the planner sees the value (a lateral join over unnest() hides it and gets a seq scan).
NAME_CANDIDATES_BRANCH = """(
  select %s::text as folded, s.entity_id
    from search_name s
    join entity e on e.id = s.entity_id and e.kind = 'company'
   where s.name %%> %s
   order by similarity(s.name, %s) desc, s.entity_id
   limit {limit})"""
NAME_CANDIDATES_CHUNK = 100  # names per query


def name_candidates_sql(n: int, limit: int) -> str:
    """The pass 3 candidate query for ``n`` folded names (params: each name three times)."""
    return "\nunion all\n".join([NAME_CANDIDATES_BRANCH.format(limit=int(limit))] * n)


NAME_CANDIDATE_LIMIT = 100  # per folded name; equal names have the highest similarity

FUZZY_CONFIDENCE = 0.8
NAME_KEYED_CONFIDENCE = 0.7
NAME_ONLY_CONFIDENCE = 0.6
AMBIGUOUS_CONFIDENCE = 0.5
MAX_MERGE_DEPTH = 20

# Identity pairs: (predicate, normalized value).
Pair = tuple[str, str]


class RejectedClaim(ValueError):
    pass


# ── results ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    id: str  # canonical (end of the merged_into chain)
    kind: str
    created_at: datetime | None  # None = minted earlier in this batch (treated as newest)

    @property
    def age_key(self) -> tuple[int, Any, str]:
        return (1, 0, self.id) if self.created_at is None else (0, self.created_at, self.id)


@dataclass(frozen=True)
class NameCandidate:
    """A company found by folded name (pass 3), with what the pass ranks it by."""

    entity: Candidate
    keyed: bool  # has a visible identity claim
    claims: int  # visible, live claims with it as subject
    safe: bool = False  # keyed and names.is_safe_name_target (wikidata / domain label / in-edges)


@dataclass
class LogRow:
    source_ref: str
    kind: str
    identity: dict[str, Any]
    decision: Decision
    entity_id: str
    confidence: float
    evidence: list[dict[str, Any]]


@dataclass
class ReviewRow:
    entity_ids: list[str]  # sorted candidates
    reason: str
    evidence: list[dict[str, Any]]
    kind: str = "ambiguous_match"


@dataclass
class Resolution:
    claims: list[ClaimWrite] = field(default_factory=list)
    logs: list[LogRow] = field(default_factory=list)
    reviews: list[ReviewRow] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (source_ref, reason)

    def count(self, decision: Decision) -> int:
        return sum(1 for r in self.logs if r.decision == decision)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "claims": len(self.claims),
            "matched": self.count("matched"),
            "minted": self.count("minted"),
            "review": self.count("review"),
            "rejected": len(self.rejected),
        }


@dataclass(frozen=True)
class Resolved:
    id: str
    minted: bool  # True = new in this batch -> written as {"mint": ...}

    @property
    def ref(self) -> WriteRef:
        if self.minted:
            return MintRef(mint=MintSpec(id=self.id, kind=self.id.split(":")[1]))
        return IdRef(id=self.id)


@dataclass(frozen=True)
class _Decision:
    resolved: Resolved
    decision: Decision
    confidence: float
    evidence: list[dict[str, Any]]
    review: ReviewRow | None = None


# ── batch state ───────────────────────────────────────────────────────────


@dataclass
class _Batch:
    conn: psycopg.Connection
    # Identity pair -> canonical candidates in the DB (prefetched for the whole batch).
    db_hits: dict[Pair, list[Candidate]] = field(default_factory=dict)
    # Batch-local cache: decisions already made in this batch.
    by_pair: dict[tuple[str, str, str], Resolved] = field(default_factory=dict)  # (kind, pred, value)
    by_name: dict[tuple[str, str, tuple[str, ...]], Resolved] = field(default_factory=dict)
    # id -> (record that minted it, its ref, evidence of the claim that minted it), for the
    # generated name claim.
    minted: dict[str, tuple[str, EntityRef, list[Evidence] | None]] = field(default_factory=dict)
    named: set[tuple[str, str]] = field(default_factory=set)  # (record, entity id) with a name claim
    known: dict[str, Candidate] = field(default_factory=dict)  # every canonical id seen, with its age
    # Facts resolved so far in this batch, for the scoped fuzzy pass (not in the DB yet).
    edges: set[tuple[str, str]] = field(default_factory=set)  # (subject id, object id)
    names: dict[str, set[str]] = field(default_factory=dict)  # id -> normalized names
    # Pass 3: folded company name -> companies in the DB with that name (prefetched), keyed
    # companies resolved earlier in this batch with that name, and the batch's decision.
    name_hits: dict[str, list[NameCandidate]] = field(default_factory=dict)
    keyed_names: dict[str, set[str]] = field(default_factory=dict)
    keyed_facts: dict[str, tuple[bool, set[str]]] = field(default_factory=dict)  # id -> (wikidata, domains)
    by_folded: dict[str, Resolved] = field(default_factory=dict)
    res: Resolution = field(default_factory=Resolution)


@dataclass(frozen=True)
class _Ref:
    """An EntityRef after identity normalization."""

    kind: str
    pairs: tuple[Pair, ...]  # sorted, de-duplicated
    name: str | None
    name_key: str  # normalize_name(name) or ""
    folded: str  # fold_name(kind, name) or ""
    context: tuple[EntityRef, ...]
    dropped: tuple[dict[str, Any], ...]  # identity values that failed normalization

    @property
    def by_company_name(self) -> bool:
        """Pass 3 applies: a company with no identity key and a real (non-placeholder) name."""
        return (
            self.kind == "company"
            and not self.pairs
            and bool(self.folded)
            and not is_placeholder_company(self.folded)
        )

    @property
    def mints_by_name(self) -> bool:
        """Pass 3/4 may converge this ref without a safe keyed target: match a name-only
        company, or mint from the folded name. Short one-word names ("base") may not."""
        return self.by_company_name and converges_by_name(self.folded)


class Resolver:
    def __init__(self, db: Db, *, source: str, layer: str, predicates: dict[str, Predicate] | None = None):
        self.db = db
        self.source = source
        self.layer = layer
        self.public_layer = layer.startswith("public/")
        if predicates is None:
            with db.view() as conn:
                predicates = load_predicates(conn)
        self.predicates = predicates

    # ── public API ──

    def resolve(self, items: Sequence[ClaimInput | tuple[str, ClaimInput]]) -> Resolution:
        """Resolve one batch. Items are ``(record_external_id, claim)`` or bare claims.

        The record id scopes identity-less mints (see ``_mint``); for a bare claim its
        ``source_ref`` is used instead.
        """
        pairs = [(c.source_ref, c) if isinstance(c, ClaimInput) else c for c in items]
        with self.db.view() as conn:
            batch = _Batch(conn=conn)
            self._prefetch(batch, (c for _, c in pairs))
            for record, claim in pairs:
                try:
                    batch.res.claims.append(self._resolve_claim(batch, record, claim))
                except RejectedClaim as e:
                    batch.res.rejected.append((claim.source_ref, str(e)))
                    log.warning("rejected claim %s: %s", claim.source_ref, e)
            self._add_name_claims(batch)
        return batch.res

    def commit(self, res: Resolution) -> None:
        """Persist resolution_log rows and (de-duplicated) open review items."""
        with self.db.view() as conn:
            if res.logs:
                with conn.cursor() as cur:
                    cur.executemany(
                        """insert into resolution_log
                             (source, source_ref, kind, identity, decision, entity_id, confidence, evidence)
                           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                        [
                            (
                                self.source,
                                r.source_ref,
                                r.kind,
                                Jsonb(r.identity),
                                r.decision,
                                r.entity_id,
                                r.confidence,
                                Jsonb(r.evidence),
                            )
                            for r in res.logs
                        ],
                    )
            for rv in res.reviews:
                # An ambiguous match is re-opened if it recurs after being closed; a possible
                # duplicate is opened once per entity set (a dismissed one stays dismissed).
                conn.execute(
                    """insert into review_item (kind, entity_ids, reason, evidence)
                       select %(kind)s, %(ids)s::text[], %(reason)s, %(evidence)s
                       where not exists (select 1 from review_item
                                          where (status = 'open' or %(once)s) and kind = %(kind)s
                                            and entity_ids = %(ids)s::text[])""",
                    {
                        "kind": rv.kind,
                        "ids": rv.entity_ids,
                        "reason": rv.reason,
                        "evidence": Jsonb(rv.evidence),
                        "once": rv.kind == "possible_duplicate",
                    },
                )

    # ── claims ──

    def _resolve_claim(self, batch: _Batch, record: str, c: ClaimInput) -> ClaimWrite:
        pred = self._check(c)
        value = c.value
        if pred.is_identity:
            try:
                value = pred.normalizer(value)
            except ValueError as e:
                raise RejectedClaim(f"identity value for {pred.id!r} not normalizable: {e}") from None
        subject = self._resolve_ref(batch, c.subject, record, c.source_ref, c.evidence)
        obj = (
            self._resolve_ref(batch, c.object, record, c.source_ref, c.evidence)
            if c.object is not None
            else None
        )
        if obj is not None and not c.retract:
            batch.edges.add((subject.id, obj.id))
        if pred.id == "name" and isinstance(value, str) and not c.retract:
            batch.names.setdefault(subject.id, set()).add(normalize_name(value))
            batch.named.add((record, subject.id))
        return ClaimWrite(
            subject=subject.ref,
            predicate=c.predicate,
            object=obj.ref if obj else None,
            value=value,
            valid_from=c.valid_from,
            valid_to=c.valid_to,
            retract=c.retract,
            source_ref=c.source_ref,
            evidence=c.evidence,
        )

    def _check(self, c: ClaimInput) -> Predicate:
        """The writer's per-claim rules (CONTRACTS §5), checked before anything is resolved."""
        pred = self.predicates.get(c.predicate)
        if pred is None:
            raise RejectedClaim(f"unknown predicate {c.predicate!r}")
        if pred.is_private and self.public_layer:
            raise RejectedClaim(f"private predicate {pred.id!r} in public layer {self.layer!r}")
        if _kind_of(c.subject) not in pred.subject_kinds:
            raise RejectedClaim(f"{pred.id!r} does not apply to subject kind {_kind_of(c.subject)!r}")
        if pred.is_edge:
            if c.object is None:
                raise RejectedClaim(f"edge {pred.id!r} needs an object")
            if pred.object_kind != "*" and _kind_of(c.object) != pred.object_kind:  # '*' = any kind
                raise RejectedClaim(f"edge {pred.id!r} needs a {pred.object_kind} object")
            if c.value is not None and not isinstance(c.value, dict):
                raise RejectedClaim(f"edge {pred.id!r} value must be an object of props or null")
        else:
            if c.object is not None:
                raise RejectedClaim(f"attribute {pred.id!r} must not have an object")
            if c.value is None:
                raise RejectedClaim(f"attribute {pred.id!r} needs a value")
            if pred.meta.get("value_type") == "json":  # e.g. milestone {title, kind, url}
                if not isinstance(c.value, dict):
                    raise RejectedClaim(f"attribute {pred.id!r} value must be a JSON object")
            elif isinstance(c.value, dict):
                raise RejectedClaim(f"attribute {pred.id!r} value must be a scalar or array")
        return pred

    def _add_name_claims(self, batch: _Batch) -> None:
        """Every entity has a name: add one for each minted entity whose ref carries a name,
        unless the record that minted it names it itself.

        The rule depends only on the minting record, never on how records were batched, so
        a replay with a different batch size writes exactly the same claims.
        """
        for entity_id, (record, ref, evidence) in batch.minted.items():
            if ref.name and (record, entity_id) not in batch.named:
                batch.res.claims.append(
                    ClaimWrite(
                        subject=Resolved(entity_id, minted=True).ref,
                        predicate="name",
                        value=ref.name,
                        source_ref=f"{self.source}:entity:{entity_id}:name",
                        # The name comes from the claim that minted the entity, so it cites the
                        # same evidence (parser/enricher workers must cite evidence on every claim).
                        evidence=evidence,
                    )
                )

    # ── entities: the three passes ──

    def _resolve_ref(
        self,
        batch: _Batch,
        ref: EntityRef,
        record: str,
        source_ref: str,
        evidence: list[Evidence] | None = None,
    ) -> Resolved:
        if ref.id is not None:
            return Resolved(ref.id, minted=False)
        r = self._normalize(ref)
        context_ids = self._context_ids(batch, r)

        cached = self._cached(batch, r, context_ids)
        if cached is not None:
            return cached  # decided (and logged) earlier in this batch

        d = (
            self._hard_keys(batch, r)
            or self._scoped_fuzzy(batch, r, context_ids)
            or self._company_name(batch, r)
            or self._mint(batch, r, record)
        )
        batch.res.logs.append(
            LogRow(
                source_ref=source_ref,
                kind=r.kind,
                identity={
                    "identity": dict(r.pairs),
                    "name": r.name,
                    "context": list(context_ids),
                    **({"invalid": list(r.dropped)} if r.dropped else {}),
                },
                decision=d.decision,
                entity_id=d.resolved.id,
                confidence=d.confidence,
                evidence=d.evidence,
            )
        )
        if d.review is not None:
            batch.res.reviews.append(d.review)
        if r.name_key:
            batch.names.setdefault(d.resolved.id, set()).add(r.name_key)
        if r.kind == "company" and r.folded:
            how = d.evidence[0].get("pass") if d.evidence else None
            if r.pairs:
                batch.keyed_names.setdefault(r.folded, set()).add(d.resolved.id)
                wd, doms = batch.keyed_facts.setdefault(d.resolved.id, (False, set()))
                doms.update(v for p, v in r.pairs if p == "domain")
                if any(p == "wikidata" for p, _ in r.pairs) and not wd:
                    batch.keyed_facts[d.resolved.id] = (True, doms)
            elif how == "company_name" or (how == "mint" and r.mints_by_name):
                batch.by_folded.setdefault(r.folded, d.resolved)
        if d.resolved.minted:
            batch.minted.setdefault(d.resolved.id, (record, ref, evidence))
            batch.known.setdefault(d.resolved.id, Candidate(d.resolved.id, r.kind, None))
        self._remember(batch, r, context_ids, d.resolved)
        return d.resolved

    def _context_ids(self, batch: _Batch, r: _Ref) -> tuple[str, ...]:
        """Ids of the ref's context entities that already exist (in the DB or this batch).

        Context is only a hint for pass 2: it is looked up by id or hard key, never minted,
        logged or sent to review.
        """
        ids: set[str] = set()
        for c in r.context:
            if c.id is not None:
                ids.add(c.id)
                continue
            cr = self._normalize(c)
            d = self._cached(batch, cr, ()) or self._hard_keys(batch, cr)
            if isinstance(d, Resolved):
                ids.add(d.id)
            elif d is not None:
                ids.add(d.resolved.id)
        return tuple(sorted(ids))

    def _hard_keys(self, batch: _Batch, r: _Ref) -> _Decision | None:
        """Pass 1: identity values that already belong to an entity (in the DB or this batch)."""
        found: dict[str, Candidate] = {}
        evidence: list[dict[str, Any]] = []
        for pred, value in r.pairs:
            hits = [c for c in batch.db_hits.get((pred, value), []) if c.kind == r.kind]
            local = batch.by_pair.get((r.kind, pred, value))
            if local is not None and local.id not in {c.id for c in hits}:
                hits.append(batch.known[local.id])
            for cand in hits:
                found.setdefault(cand.id, cand)
                evidence.append({"pass": "hard_key", "predicate": pred, "value": value, "entity": cand.id})
        if not found:
            return None
        return self._pick(batch, r, list(found.values()), evidence, confidence=1.0, how="identity key")

    def _scoped_fuzzy(self, batch: _Batch, r: _Ref, context_ids: tuple[str, ...]) -> _Decision | None:
        """Pass 2: same kind + same normalized name + an edge (either direction) to a context entity."""
        if not context_ids or not r.name_key:
            return None
        rows = batch.conn.execute(
            """with neighbor as (
                 select case when e.object_id = any(%(ctx)s) then e.subject else e.object_id end as id
                   from claim e
                  where e.object_id is not null and not e.retract
                    and (e.object_id = any(%(ctx)s) or e.subject = any(%(ctx)s))
               )
               select distinct nb.id, n.value_text as name
                 from neighbor nb
                 join claim n on n.subject = nb.id and n.predicate = 'name'
                             and n.object_id is null and not n.retract
                where nb.id <> all(%(ctx)s)""",
            {"ctx": list(context_ids)},
        ).fetchall()
        same_name = {row["id"] for row in rows if normalize_name(row["name"] or "") == r.name_key}
        # ... plus edges and names resolved earlier in this batch (not written yet).
        ctx = set(context_ids)
        local = {o if s in ctx else s for s, o in batch.edges if s in ctx or o in ctx} - ctx
        local_same = {i for i in local if r.name_key in batch.names.get(i, ())}
        candidates = self._canonicalize(batch, same_name | (local_same - batch.known.keys()))
        candidates.update({i: batch.known[i] for i in local_same if i in batch.known})
        found = {c.id: c for c in candidates.values() if c.kind == r.kind and c.id not in ctx}
        if not found:
            return None
        evidence = [
            {"pass": "scoped_fuzzy", "name": r.name_key, "context": list(context_ids), "entity": cid}
            for cid in sorted(found)
        ]
        return self._pick(
            batch, r, list(found.values()), evidence, confidence=FUZZY_CONFIDENCE, how="name within context"
        )

    def _company_name(self, batch: _Batch, r: _Ref) -> _Decision | None:
        """Pass 3: an identity-less company ref -> an existing company with the same folded name.

        Candidates: companies in the DB (``_prefetch_names``) plus keyed companies resolved
        earlier in this batch. A keyed candidate is only a target when it is *safe*
        (``names.is_safe_name_target``): exact names alone link generic words ("Capital",
        "Base") to unrelated companies. Without a safe one, the ref may still converge on a
        name-only company, unless its name is too short (``names.converges_by_name``).
        ``None`` -> pass 4 (a name mint, or a record-scoped one for short names).
        """
        if not r.by_company_name:
            return None
        decided = batch.by_folded.get(r.folded)
        if decided is not None:
            return _Decision(
                decided,
                "matched",
                NAME_ONLY_CONFIDENCE,
                [{"pass": "company_name", "name": r.folded, "entity": decided.id, "batch": True}],
            )
        found: dict[str, NameCandidate] = {c.entity.id: c for c in batch.name_hits.get(r.folded, [])}
        for eid in sorted(batch.keyed_names.get(r.folded, ())):
            cand = batch.known.get(eid) or Candidate(eid, "company", None)
            prev = found.get(cand.id)
            wikidata, domains = batch.keyed_facts.get(eid, (False, set()))
            safe = (prev is not None and prev.safe) or is_safe_name_target(
                r.folded, has_wikidata=wikidata, domains=domains
            )
            found[cand.id] = NameCandidate(cand, True, prev.claims if prev else 0, safe)
        if not found:
            return None
        keyed = sorted((c for c in found.values() if c.keyed), key=lambda c: c.entity.age_key)
        safe = [c for c in keyed if c.safe]
        evidence = [
            {
                "pass": "company_name",
                "name": r.folded,
                "entity": c.entity.id,
                "keyed": c.keyed,
                "safe": c.safe,
                "claims": c.claims,
            }
            for c in sorted(found.values(), key=lambda c: c.entity.id)
        ]
        if safe:
            chosen = safe[0].entity  # the oldest safe one
            resolved = Resolved(chosen.id, minted=chosen.created_at is None)
            if len(keyed) == 1:
                return _Decision(resolved, "matched", NAME_KEYED_CONFIDENCE, evidence)
            ids = sorted(c.entity.id for c in keyed)
            review = ReviewRow(
                entity_ids=ids,
                reason=f"{len(ids)} companies with identity keys are named {r.folded!r}; "
                f"used the oldest safe match ({chosen.id})",
                evidence=evidence,
                kind="possible_duplicate",
            )
            return _Decision(resolved, "review", AMBIGUOUS_CONFIDENCE, evidence, review)
        if not r.mints_by_name:
            return None  # a short generic name: no convergence without a safe keyed target
        # Name-only companies (unsafe keyed ones are never targets): the best-attested, then oldest.
        name_only = [c for c in found.values() if not c.keyed]
        if not name_only:
            return None
        best = min(name_only, key=lambda c: (-c.claims, c.entity.age_key)).entity
        return _Decision(
            Resolved(best.id, minted=best.created_at is None), "matched", NAME_ONLY_CONFIDENCE, evidence
        )

    def _mint(self, batch: _Batch, r: _Ref, record: str) -> _Decision:
        """Pass 4: a deterministic id (CONTRACTS §3).

        A company eligible for pass 3 whose name may converge (``_Ref.mints_by_name``) is keyed
        by its folded name, so every record that names it converges on one id. Other
        identity-less entities (persons, short company names, placeholders) are keyed by
        ``{record}#{kind}:{normalized name}``: stable across replays, and distinct for two
        unnamed-key entities in the same record.
        """
        identity: dict[str, list[str]] = {}
        for p, v in r.pairs:
            identity.setdefault(p, []).append(v)
        if r.mints_by_name:
            identity[NAME_KEY] = [r.folded]
        ref_key = f"{record}#{r.kind}:{r.name_key}"
        entity_id = mint_id(r.kind, identity, self.source, ref_key)

        existing = self._canonicalize(batch, [entity_id]).get(entity_id)
        if existing is not None:
            # An earlier run minted this id (e.g. an identity-less entity): reuse it.
            return _Decision(
                Resolved(existing.id, minted=False),
                "matched",
                1.0,
                [{"pass": "mint", "existing": True, "minted_id": entity_id}],
            )
        if r.pairs:
            evidence: dict[str, Any] = {"pass": "mint", "basis": "identity"}
        elif r.mints_by_name:
            evidence = {"pass": "mint", "basis": "name", "name": r.folded}
        else:
            evidence = {"pass": "mint", "basis": "source_ref", "ref": ref_key}
        return _Decision(Resolved(entity_id, minted=True), "minted", 1.0, [evidence])

    def _pick(
        self,
        batch: _Batch,
        r: _Ref,
        candidates: list[Candidate],
        evidence: list[dict[str, Any]],
        *,
        confidence: float,
        how: str,
    ) -> _Decision:
        """One candidate -> matched. Several -> the oldest, plus an ambiguous_match review item."""
        candidates.sort(key=lambda c: c.age_key)
        chosen = candidates[0]
        resolved = Resolved(chosen.id, minted=chosen.created_at is None)
        if len(candidates) == 1:
            return _Decision(resolved, "matched", confidence, evidence)
        ids = sorted(c.id for c in candidates)
        review = ReviewRow(
            entity_ids=ids,
            reason=f"{len(ids)} {r.kind} entities match by {how}; used the oldest ({chosen.id})",
            evidence=evidence,
        )
        return _Decision(resolved, "review", AMBIGUOUS_CONFIDENCE, evidence, review)

    def _canonicalize(self, batch: _Batch, ids: Iterable[str]) -> dict[str, Candidate]:
        out = _canonicalize(batch.conn, ids)
        for c in out.values():
            batch.known.setdefault(c.id, c)
        return out

    # ── batch cache ──

    def _cached(self, batch: _Batch, r: _Ref, context_ids: tuple[str, ...]) -> Resolved | None:
        """An entity this batch already resolved: same identity value, or same identity-less
        (kind, name, context). Pass 1 handles refs that hit the cache on only some keys."""
        if r.pairs:
            hits = {batch.by_pair.get((r.kind, p, v)) for p, v in r.pairs} - {None}
            fully_cached = all((r.kind, p, v) in batch.by_pair for p, v in r.pairs)
            if len(hits) == 1 and fully_cached:
                return hits.pop()
            return None
        return batch.by_name.get((r.kind, r.name_key, context_ids)) if r.name_key else None

    def _remember(self, batch: _Batch, r: _Ref, context_ids: tuple[str, ...], resolved: Resolved) -> None:
        for p, v in r.pairs:
            batch.by_pair.setdefault((r.kind, p, v), resolved)
        if not r.pairs and r.name_key:
            batch.by_name.setdefault((r.kind, r.name_key, context_ids), resolved)

    # ── identity values ──

    def _normalize(self, ref: EntityRef) -> _Ref:
        assert ref.kind is not None
        pairs: set[Pair] = set()
        dropped: list[dict[str, Any]] = []
        for pred_id, raw in ref.identity.items():
            pred = self.predicates.get(pred_id)
            if pred is None or not pred.is_identity:
                dropped.append({"predicate": pred_id, "value": raw, "error": "not an identity predicate"})
                continue
            for item in raw if isinstance(raw, list) else [raw]:
                try:
                    pairs.add((pred_id, pred.normalizer(item)))
                except ValueError as e:
                    dropped.append({"predicate": pred_id, "value": item, "error": str(e)})
        return _Ref(
            kind=ref.kind,
            pairs=tuple(sorted(pairs)),
            name=ref.name,
            name_key=normalize_name(ref.name) if ref.name else "",
            folded=fold_name(ref.kind, ref.name),
            context=tuple(ref.context or ()),
            dropped=tuple(dropped),
        )

    def _prefetch(self, batch: _Batch, claims: Iterable[ClaimInput]) -> None:
        """Look up every identity value of the batch in one query (``HARD_KEY_SQL``).

        Only predicates with ``meta.identity = true`` reach here (``_normalize`` drops others).
        """
        wanted: set[Pair] = set()
        folded: set[str] = set()

        def walk(ref: EntityRef | None, *, context: bool = False) -> None:
            if ref is None or ref.id is not None:
                return
            r = self._normalize(ref)
            wanted.update(r.pairs)
            if r.by_company_name and not context:  # context refs are never name-matched
                folded.add(r.folded)
            for c in ref.context or ():
                walk(c, context=True)

        for c in claims:
            walk(c.subject)
            walk(c.object)
        self._prefetch_names(batch, folded)
        if not wanted:
            return
        preds, values = zip(*sorted(wanted), strict=True)
        rows = batch.conn.execute(HARD_KEY_SQL, (list(preds), list(values))).fetchall()
        canon = self._canonicalize(batch, {row["subject"] for row in rows})
        for row in rows:
            cand = canon.get(row["subject"])
            if cand is None:
                continue
            hits = batch.db_hits.setdefault((row["predicate"], row["value"]), [])
            if cand not in hits:
                hits.append(cand)

    def _prefetch_names(self, batch: _Batch, folded: set[str]) -> None:
        """Pass 3 lookup for the whole batch: folded company name -> ``NameCandidate``s.

        Four queries (plus one per 100 names): candidates by trigram (``name_candidates_sql``),
        every visible name claim of the candidates (folded and compared here), then for the
        canonical entities their claim count, identity keys (keyed? wikidata? domains?) and
        incoming non-``follows`` edges, which decide ``NameCandidate.safe``.
        """
        if not folded:
            return
        conn = batch.conn
        names_list = sorted(folded)
        rows: list[dict[str, Any]] = []
        for i in range(0, len(names_list), NAME_CANDIDATES_CHUNK):
            chunk = names_list[i : i + NAME_CANDIDATES_CHUNK]
            sql = name_candidates_sql(len(chunk), NAME_CANDIDATE_LIMIT)
            rows += conn.execute(sql, [v for f in chunk for v in (f, f, f)]).fetchall()
        raw_ids = sorted({row["entity_id"] for row in rows})
        if not raw_ids:
            return
        names = conn.execute(
            """select subject, value_text as name from claim
                where subject = any(%s::text[]) and predicate = 'name'
                  and object_id is null and not retract""",
            (raw_ids,),
        ).fetchall()
        canon = self._canonicalize(batch, raw_ids)
        # folded name -> canonical ids of the companies that carry it
        matches: dict[str, set[str]] = {}
        for row in names:
            f = fold_name("company", row["name"] or "")
            cand = canon.get(row["subject"])
            if f in folded and cand is not None and cand.kind == "company":
                matches.setdefault(f, set()).add(cand.id)
        ids = sorted(set().union(*matches.values())) if matches else []
        if not ids:
            return
        identity_preds = sorted(p.id for p in self.predicates.values() if p.is_identity)
        stats = {
            row["subject"]: row
            for row in conn.execute(
                """select subject, count(*)::int as claims,
                          bool_or(object_id is null and predicate = any(%(idp)s::text[])) as keyed,
                          bool_or(object_id is null and predicate = 'wikidata') as wikidata,
                          array_agg(distinct value_text)
                            filter (where object_id is null and predicate = 'domain') as domains
                     from claim where subject = any(%(ids)s::text[]) and not retract
                    group by subject""",
                {"idp": identity_preds, "ids": ids},
            ).fetchall()
        }
        # Safety rule (c): distinct subjects with a non-`follows` edge to the candidate.
        incoming = {
            row["object_id"]: row["n"]
            for row in conn.execute(
                """select object_id, count(distinct subject)::int as n
                     from claim where object_id = any(%s::text[]) and predicate <> 'follows' and not retract
                    group by object_id""",
                (ids,),
            ).fetchall()
        }
        for f, cids in matches.items():
            hits: list[NameCandidate] = []
            for cid in sorted(cids):
                st = stats.get(cid) or {}
                keyed = bool(st.get("keyed"))
                safe = keyed and is_safe_name_target(
                    f,
                    has_wikidata=bool(st.get("wikidata")),
                    domains=[d for d in st.get("domains") or () if d],
                    incoming_subjects=incoming.get(cid, 0),
                )
                hits.append(NameCandidate(batch.known[cid], keyed, int(st.get("claims") or 0), safe))
            batch.name_hits[f] = hits


def _kind_of(ref: EntityRef) -> str | None:
    if ref.id is not None:
        parts = ref.id.split(":")
        return parts[1] if len(parts) == 3 else None
    return ref.kind


def _canonicalize(conn: psycopg.Connection, ids: Iterable[str]) -> dict[str, Candidate]:
    """id -> the entity at the end of its merged_into chain (only visible entities)."""
    ids = sorted(set(ids))
    if not ids:
        return {}
    rows = conn.execute(
        """with recursive chain as (
             select e.id as start, e.id, e.merged_into, e.kind, e.created_at, 0 as depth
               from entity e where e.id = any(%s::text[])
             union all
             select ch.start, e.id, e.merged_into, e.kind, e.created_at, ch.depth + 1
               from chain ch join entity e on e.id = ch.merged_into
              where ch.depth < %s
           )
           select distinct on (start) start, id, kind, created_at
             from chain where merged_into is null
            order by start, depth desc""",
        (ids, MAX_MERGE_DEPTH),
    ).fetchall()
    return {r["start"]: Candidate(r["id"], r["kind"], r["created_at"]) for r in rows}
