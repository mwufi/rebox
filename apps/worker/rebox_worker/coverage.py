"""Coverage checker: how much of a list of people/companies does Rebox already know?

``rebox-worker coverage FILE.csv [--out report.csv] [--json] [--enqueue]``

Input is a CSV with a header. Recognised columns (case-insensitive, any subset): ``kind``,
``name`` (or ``first_name`` + ``last_name``), ``x`` / ``twitter`` / ``x_handle``, ``github``,
``domain``, ``website`` / ``url``, ``wikidata``, ``linkedin`` (echoed, never matched) and
``company`` (people: helps disambiguate). Every other column is passed through to the report.

Matching is read-only (the matching transaction is ``read only``) and runs as ``rebox_app`` with
all public layers visible, like the pipeline:

1. **Hard keys**, normalized with the vocabulary's normalizers exactly as the resolver does, looked
   up in one batched query (``resolve.HARD_KEY_SQL``) and followed through ``merged_into``
   (``resolve._canonicalize``): ``x_handle``, ``github``, ``domain`` (from ``domain`` or
   ``website``), ``wikidata``, and ``website`` (URL form). Only entities of the row's kind count;
   several entities -> the oldest (as the resolver picks), the others listed as candidates.
2. **Name**: candidates from ``search_name`` (trigram), scored 1.0 for an equal folded name
   (``normalize.normalize_name``; companies also ignore a trailing legal form; "Name (Alias)"
   tries both) else trigram similarity; kept if >= 0.6. A candidate with a different X handle /
   GitHub login than the row's is a namesake and dropped. For a person with a ``company``,
   candidates with a ``worked_at``/``founded`` edge to a company of that name win. Two or more
   candidates within 0.05 of the best -> ``ambiguous``.

Statuses: ``matched`` (hard key), ``matched_name``, ``ambiguous``, ``missing``.

``--enqueue`` puts missing and thin (<= 3 facts) rows into ``frontier``: rows with an X handle as
``source='x_spider'`` (key ``@<handle>``: the spider keys accounts by X user id, which a list does
not have; depth 0 keeps the spider's expansion/posts phases from treating it as an id), the rest
as ``source='exa'`` (key = domain, else the name). It never fetches anything.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg

from . import frontier
from .db import Db
from .normalize import normalize_name
from .predicates import Predicate, load_predicates
from .resolve import HARD_KEY_SQL, Candidate, _canonicalize

STATUSES = ("matched", "matched_name", "ambiguous", "missing")
KINDS = ("person", "company")
OUTPUT_COLUMNS = (
    "status",
    "entity_id",
    "rebox_name",
    "facts",
    "edges",
    "sources",
    "timeline",
    "url",
    "candidates",
)

NAME_MIN_SCORE = 0.6  # trigram similarity a name candidate needs
AMBIGUOUS_DELTA = 0.05  # candidates this close to the best one make the row ambiguous
RETRIEVE_THRESHOLD = 0.4  # pg_trgm threshold for fetching candidates (scored in Python after)
RETRIEVE_LIMIT = 50  # candidates per name
THIN_FACTS = 3  # a match with this many facts or fewer is "thin"
COMPANY_EDGES = ("worked_at", "founded")
CHUNK = 2000  # rows per batched query
DEFAULT_EXPLORER_URL = "http://localhost:3000"

# Recognised columns (lowercased header -> field).
ALIASES: dict[str, str] = {
    "kind": "kind",
    "type": "kind",
    "name": "name",
    "full_name": "name",
    "first_name": "first_name",
    "last_name": "last_name",
    "x": "x",
    "twitter": "x",
    "x_handle": "x",
    "github": "github",
    "domain": "domain",
    "website": "website",
    "url": "website",
    "wikidata": "wikidata",
    "linkedin": "linkedin",
    "company": "company",
}
PERSON_HINT_COLUMNS = ("first_name", "last_name", "title", "role", "job_title")
KIND_WORDS = {
    "person": "person", "people": "person", "founder": "person", "individual": "person",
    "company": "company", "companies": "company", "org": "company", "organization": "company",
    "organisation": "company", "startup": "company",
}  # fmt: skip
# Hosts whose URLs are profiles on a platform, not a company's own site (never a `domain` key).
PLATFORM_HOSTS = frozenset({
    "x.com", "twitter.com", "linkedin.com", "github.com", "facebook.com", "instagram.com",
    "youtube.com", "medium.com", "substack.com", "crunchbase.com", "angel.co", "wellfound.com",
    "producthunt.com", "ycombinator.com", "notion.site", "linktr.ee",
})  # fmt: skip


# ── input ────────────────────────────────────────────────────────────────


@dataclass
class InputRow:
    n: int  # 1-based data row number
    raw: dict[str, str]
    kind: str
    kind_guessed: bool
    name: str | None = None
    company: str | None = None
    x_handle: str | None = None
    domain: str | None = None
    pairs: list[tuple[str, str]] = field(default_factory=list)  # (identity predicate, normalized value)
    invalid: list[str] = field(default_factory=list)

    @property
    def name_key(self) -> str:
        return fold_name(self.name, self.kind) if self.name else ""

    @property
    def name_variants(self) -> list[str]:
        """The name, plus both halves of "Name (Alias)" ("Anysphere (Cursor)")."""
        if not self.name:
            return []
        out = [self.name]
        m = _ALIAS_RE.match(self.name)
        if m:
            out += [m.group(1).strip(), m.group(2).strip()]
        return [v for v in dict.fromkeys(out) if v]

    @property
    def name_keys(self) -> set[str]:
        return {k for k in (fold_name(v, self.kind) for v in self.name_variants) if k}

    @property
    def label(self) -> str:
        return self.name or self.x_handle or self.domain or f"row {self.n}"


_ALIAS_RE = re.compile(r"^\s*([^()]+?)\s*\(([^()]+)\)\s*$")
# Legal-form suffixes ignored when comparing company names ("Figma, Inc." == "Figma").
COMPANY_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "plc", "pbc",
    "sa", "sas", "bv", "ag",
})  # fmt: skip


def fold_name(name: str, kind: str) -> str:
    """``normalize.normalize_name`` (the resolver's name key); for companies also without a trailing
    legal form ("Anysphere, Inc." -> "anysphere")."""
    key = normalize_name(name)
    if kind == "company":
        words = key.split()
        while len(words) > 1 and words[-1] in COMPANY_SUFFIXES:
            words.pop()
        key = " ".join(words)
    return key


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path}: empty file or no header")
        fieldnames = [h if h is not None else "" for h in reader.fieldnames]
        rows = [{k: (v or "") for k, v in r.items() if k is not None} for r in reader]
    return fieldnames, rows


def parse_rows(
    fieldnames: Sequence[str], raw_rows: Iterable[dict[str, str]], predicates: dict[str, Predicate]
) -> list[InputRow]:
    colmap: dict[str, list[str]] = {}  # field -> header names, in header order
    for h in fieldnames:
        f = ALIASES.get(h.strip().lower())
        if f:
            colmap.setdefault(f, []).append(h)
    hint_cols = [h for h in fieldnames if h.strip().lower() in PERSON_HINT_COLUMNS]
    return [_parse_row(i, raw, colmap, hint_cols, predicates) for i, raw in enumerate(raw_rows, start=1)]


def _values(raw: dict[str, str], cols: Sequence[str]) -> list[str]:
    out: list[str] = []
    for c in cols:
        for part in (raw.get(c) or "").replace(";", ",").split(","):
            if part.strip():
                out.append(part.strip())
    return out


def _first(raw: dict[str, str], cols: Sequence[str]) -> str | None:
    for c in cols:
        v = (raw.get(c) or "").strip()
        if v:
            return v
    return None


def _parse_row(
    n: int,
    raw: dict[str, str],
    colmap: dict[str, list[str]],
    hint_cols: Sequence[str],
    predicates: dict[str, Predicate],
) -> InputRow:
    def col(f: str) -> list[str]:
        return colmap.get(f, [])

    name = _first(raw, col("name"))
    if not name:
        parts = [_first(raw, col("first_name")), _first(raw, col("last_name"))]
        name = " ".join(p for p in parts if p) or None
    company = _first(raw, col("company"))
    linkedin = _first(raw, col("linkedin")) or ""
    websites = _values(raw, col("website"))
    domains = _values(raw, col("domain"))

    kind_raw = (_first(raw, col("kind")) or "").lower()
    kind = KIND_WORDS.get(kind_raw)
    guessed = kind is None
    if kind is None:
        personish = bool(company) or "/in/" in linkedin.lower() or any(_first(raw, [h]) for h in hint_cols)
        kind = "company" if (domains or websites) and not personish else "person"

    row = InputRow(n=n, raw=raw, kind=kind, kind_guessed=guessed, name=name, company=company)

    def add(pred_id: str, value: str, *, platform_guard: bool = False) -> str | None:
        pred = predicates.get(pred_id)
        if pred is None or not pred.is_identity or kind not in pred.subject_kinds:
            return None
        try:
            v = pred.normalizer(value)
        except ValueError:
            row.invalid.append(f"{pred_id}={value}")
            return None
        if platform_guard and _is_platform(v):
            return None
        if (pred_id, v) not in row.pairs:
            row.pairs.append((pred_id, v))
        return v

    for v in _values(raw, col("x")):
        h = add("x_handle", v)
        row.x_handle = row.x_handle or h
    for v in _values(raw, col("github")):
        add("github", v)
    for v in _values(raw, col("wikidata")):
        add("wikidata", v)
    for v in domains:
        d = add("domain", v)
        row.domain = row.domain or d
    for v in websites:
        d = add("domain", v, platform_guard=True)
        row.domain = row.domain or d
        add("website", v)
    return row


def _is_platform(host: str) -> bool:
    return any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


# ── matching ─────────────────────────────────────────────────────────────


@dataclass
class Match:
    status: str
    entity_id: str | None = None
    how: str | None = None  # identity predicate, "name" or "name+company"
    score: float | None = None
    candidates: list[tuple[str, float | None]] = field(default_factory=list)  # (id, score)


@dataclass
class EntityStats:
    name: str | None = None
    facts: int = 0
    edges: int = 0
    sources: list[str] = field(default_factory=list)
    timeline: bool = False


@dataclass
class RowResult:
    row: InputRow
    match: Match
    stats: EntityStats | None = None

    @property
    def known(self) -> bool:
        return self.match.status in ("matched", "matched_name")

    @property
    def thin(self) -> bool:
        return self.known and self.stats is not None and self.stats.facts <= THIN_FACTS


NAME_SQL = """
select q.i, s.entity_id, s.name, similarity(s.name, q.q) as sim
  from unnest(%s::int[], %s::text[]) as q(i, q)
  cross join lateral (
        select s.entity_id, s.name
          from search_name s
         where s.name %% q.q and s.layer_id = any (rebox_visible())
         order by similarity(s.name, q.q) desc, s.entity_id
         limit %s
  ) as s
"""

CONFLICT_KEYS = ("x_handle", "github")

IDENTITY_OF_SQL = """
select c.subject, c.predicate, c.value_text
  from claim c
 where c.subject = any (%s::text[]) and c.predicate = any (%s::text[])
   and c.object_id is null and not c.retract
"""

COMPANY_EDGE_SQL = """
select c.subject, n.value_text as company
  from claim c
  join claim n on n.subject = c.object_id and n.predicate = 'name'
              and n.object_id is null and not n.retract
 where c.subject = any (%s::text[]) and c.predicate = any (%s::text[])
   and c.object_id is not null and not c.retract
"""

GROUP_CTE = """
with recursive grp(root, id, depth) as (
  select r, r, 0 from unnest(%(ids)s::text[]) as r
  union all
  select g.root, e.id, g.depth + 1 from grp g join entity e on e.merged_into = g.id where g.depth < 20
)
"""

# Facts and sources: claims with the entity (or an entity merged into it) as subject.
OUT_SQL = (
    GROUP_CTE
    + """
select g.root,
       count(distinct c.predicate || '|' || coalesce(c.value_text, c.value::text, ''))
         filter (where c.object_id is null) as facts,
       count(distinct c.predicate || '|' || c.object_id)
         filter (where c.object_id is not null and c.predicate <> 'follows') as edges,
       array_agg(distinct c.layer_id) as layers,
       coalesce(bool_or(c.predicate in ('milestone', 'founded_year')
                        or ((c.valid_from is not null or c.valid_to is not null)
                            and c.predicate not in ('follows', 'name'))), false) as timeline
  from grp g join claim c on c.subject = g.id and not c.retract
 group by g.root
"""
)

# Incoming edges (``follows`` excluded: social-graph noise, not knowledge about the entity).
IN_SQL = (
    GROUP_CTE
    + """
select g.root,
       count(distinct c.predicate || '|' || c.subject) as edges,
       array_agg(distinct c.layer_id) as layers,
       coalesce(bool_or(c.valid_from is not null or c.valid_to is not null), false) as timeline
  from grp g join claim c on c.object_id = g.id and not c.retract and c.predicate <> 'follows'
 group by g.root
"""
)

# The displayed name: like the fold for public layers (worker trust, newest, the root's own claim).
NAME_OF_SQL = (
    GROUP_CTE
    + """
select distinct on (g.root) g.root, c.value_text as name
  from grp g
  join claim c on c.subject = g.id and c.predicate = 'name' and c.object_id is null and not c.retract
  left join worker w on w.id = c.worker_id
 order by g.root, coalesce(w.trust, 0.5) desc, c.updated_at desc, (c.subject = g.root) desc, c.id desc
"""
)


def _chunks(seq: Sequence[Any], n: int = CHUNK) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def match_rows(conn: psycopg.Connection, rows: Sequence[InputRow]) -> list[Match]:
    """Statuses for every row (read-only; ``conn`` has the view set)."""
    out: dict[int, Match] = {}

    # 1. hard keys, one batched lookup per chunk
    wanted = sorted({p for r in rows for p in r.pairs})
    hits: dict[tuple[str, str], set[str]] = {}
    for part in _chunks(wanted):
        preds, values = zip(*part, strict=True)
        for h in conn.execute(HARD_KEY_SQL, (list(preds), list(values))).fetchall():
            hits.setdefault((h["predicate"], h["value"]), set()).add(h["subject"])
    canon = _canonicalize_all(conn, {s for ss in hits.values() for s in ss})
    for r in rows:
        found: dict[str, tuple[Candidate, str]] = {}
        for pair in r.pairs:
            for s in hits.get(pair, ()):
                c = canon.get(s)
                if c is not None and c.kind == r.kind:
                    found.setdefault(c.id, (c, pair[0]))
        if found:
            ordered = sorted(found.values(), key=lambda cp: cp[0].age_key)
            best, how = ordered[0]
            out[r.n] = Match(
                "matched",
                best.id,
                how=how,
                score=1.0,
                candidates=[(c.id, 1.0) for c, _ in ordered] if len(ordered) > 1 else [],
            )

    # 2. names
    pending = [r for r in rows if r.n not in out and r.name_key]
    by_row = _name_candidates(conn, pending)
    with_company = [r for r in pending if r.kind == "person" and r.company and by_row.get(r.n)]
    company_names = _company_names(conn, [c for r in with_company for c in by_row[r.n].values()])
    for r in pending:
        out[r.n] = _decide_name(r, by_row.get(r.n, {}), company_names)
    return [out.get(r.n) or Match("missing") for r in rows]


def _canonicalize_all(conn: psycopg.Connection, ids: Iterable[str]) -> dict[str, Candidate]:
    res: dict[str, Candidate] = {}
    for part in _chunks(sorted(set(ids))):
        res.update(_canonicalize(conn, part))
    return res


@dataclass
class _NameCand:
    entity: str  # canonical id
    members: set[str]  # raw ids (search_name rows) that lead to it
    score: float  # 1.0 = equal folded name, else best trigram similarity


def _name_candidates(conn: psycopg.Connection, rows: Sequence[InputRow]) -> dict[int, dict[str, _NameCand]]:
    """row number -> canonical id -> candidate (same kind, score >= NAME_MIN_SCORE)."""
    if not rows:
        return {}
    conn.execute("select set_config('pg_trgm.similarity_threshold', %s, true)", (str(RETRIEVE_THRESHOLD),))
    queries: list[tuple[int, str]] = []
    for r in rows:
        for v in r.name_variants:
            queries.append((r.n, v))
            folded = normalize_name(v)
            if folded and folded != v.lower():  # accents/punctuation: also search the folded form
                queries.append((r.n, folded))
    raw: list[dict[str, Any]] = []
    for part in _chunks(queries):
        ns, qs = zip(*part, strict=True)
        raw += conn.execute(NAME_SQL, (list(ns), list(qs), RETRIEVE_LIMIT)).fetchall()
    canon = _canonicalize_all(conn, {h["entity_id"] for h in raw})
    kinds = {r.n: r for r in rows}
    out: dict[int, dict[str, _NameCand]] = {}
    for h in raw:
        r = kinds[h["i"]]
        c = canon.get(h["entity_id"])
        if c is None or c.kind != r.kind:
            continue
        score = 1.0 if fold_name(h["name"], r.kind) in r.name_keys else float(h["sim"])
        if score < NAME_MIN_SCORE:
            continue
        cands = out.setdefault(r.n, {})
        nc = cands.get(c.id)
        if nc is None:
            cands[c.id] = _NameCand(c.id, {h["entity_id"]}, score)
        else:
            nc.members.add(h["entity_id"])
            nc.score = max(nc.score, score)
    _drop_conflicts(conn, rows, out)
    return out


def _drop_conflicts(
    conn: psycopg.Connection, rows: Sequence[InputRow], by_row: dict[int, dict[str, _NameCand]]
):
    """Drop name candidates that have a different X handle / GitHub login than the row gives: a
    namesake, not the same entity ("David Hsu" @dvdhsu is not "David" @davidtsong). Domains are not
    used here (companies often have several)."""
    rows_with = [r for r in rows if r.n in by_row and any(p in CONFLICT_KEYS for p, _ in r.pairs)]
    if not rows_with:
        return
    member_of: dict[str, set[str]] = {}
    for r in rows_with:
        for c in by_row[r.n].values():
            for m in c.members | {c.entity}:
                member_of.setdefault(m, set()).add(c.entity)
    values: dict[str, dict[str, set[str]]] = {}  # canonical id -> predicate -> values
    for part in _chunks(sorted(member_of)):
        for row in conn.execute(IDENTITY_OF_SQL, (list(part), list(CONFLICT_KEYS))).fetchall():
            for e in member_of[row["subject"]]:
                values.setdefault(e, {}).setdefault(row["predicate"], set()).add(row["value_text"])
    for r in rows_with:
        mine: dict[str, set[str]] = {}
        for p, v in r.pairs:
            if p in CONFLICT_KEYS:
                mine.setdefault(p, set()).add(v)
        cands = by_row[r.n]
        for eid in list(cands):
            theirs = values.get(eid, {})
            if any(theirs.get(p) and not (theirs[p] & vs) for p, vs in mine.items()):
                del cands[eid]
        if not cands:
            del by_row[r.n]


def _company_names(conn: psycopg.Connection, cands: Iterable[_NameCand]) -> dict[str, set[str]]:
    """canonical person id -> folded names of companies they worked at / founded."""
    member_of = {m: c.entity for c in cands for m in c.members | {c.entity}}
    out: dict[str, set[str]] = {}
    for part in _chunks(sorted(member_of)):
        for row in conn.execute(COMPANY_EDGE_SQL, (list(part), list(COMPANY_EDGES))).fetchall():
            if row["company"]:
                out.setdefault(member_of[row["subject"]], set()).add(fold_name(row["company"], "company"))
    return out


def _company_matches(given: str, known: Iterable[str]) -> bool:
    """Equal folded names, or one is the other plus extra words ("Ditto" ~ "Ditto AI")."""
    return any(
        k == given or k.startswith(given + " ") or given.startswith(k + " ") for k in known if k and given
    )


def _decide_name(r: InputRow, cands: dict[str, _NameCand], company_names: dict[str, set[str]]) -> Match:
    if not cands:
        return Match("missing")
    pool = list(cands.values())
    how = "name"
    if r.kind == "person" and r.company:
        given = fold_name(r.company, "company")
        preferred = [c for c in pool if _company_matches(given, company_names.get(c.entity, ()))]
        if preferred:
            pool, how = preferred, "name+company"
    pool.sort(key=lambda c: (-c.score, c.entity))
    best = pool[0]
    close = [c for c in pool if best.score - c.score <= AMBIGUOUS_DELTA]
    if len(close) > 1:
        return Match("ambiguous", how=how, score=best.score, candidates=[(c.entity, c.score) for c in close])
    return Match("matched_name", best.entity, how=how, score=best.score)


def entity_stats(conn: psycopg.Connection, ids: Iterable[str]) -> dict[str, EntityStats]:
    out: dict[str, EntityStats] = {}
    for part in _chunks(sorted(set(ids))):
        params = {"ids": list(part)}
        for i in part:
            out[i] = EntityStats()
        for row in conn.execute(NAME_OF_SQL, params).fetchall():
            out[row["root"]].name = row["name"]
        layers: dict[str, set[str]] = {i: set() for i in part}
        for row in conn.execute(OUT_SQL, params).fetchall():
            s = out[row["root"]]
            s.facts, s.edges, s.timeline = row["facts"], row["edges"], bool(row["timeline"])
            layers[row["root"]].update(row["layers"] or ())
        for row in conn.execute(IN_SQL, params).fetchall():
            s = out[row["root"]]
            s.edges += row["edges"]
            s.timeline = s.timeline or bool(row["timeline"])
            layers[row["root"]].update(row["layers"] or ())
        for i, ls in layers.items():
            out[i].sources = sorted(ls)
    return out


@dataclass
class Coverage:
    results: list[RowResult]
    entities: dict[str, EntityStats]  # every matched or candidate entity (for names)


def check(db: Db, rows: Sequence[InputRow]) -> Coverage:
    """Match every row and gather stats for matches and candidates, in one read-only transaction."""
    with db.view() as conn:
        conn.execute("set transaction read only")
        matches = match_rows(conn, rows)
        ids = {m.entity_id for m in matches if m.entity_id} | {c for m in matches for c, _ in m.candidates}
        stats = entity_stats(conn, ids)
    results = [
        RowResult(r, m, stats.get(m.entity_id) if m.entity_id else None)
        for r, m in zip(rows, matches, strict=True)
    ]
    return Coverage(results, stats)


# ── output ───────────────────────────────────────────────────────────────


def entity_url(entity_id: str, explorer_url: str) -> str:
    return f"{explorer_url.rstrip('/')}/e/{quote(entity_id, safe='')}"


def report_row(res: RowResult, explorer_url: str, names: dict[str, EntityStats]) -> dict[str, str]:
    m, s = res.match, res.stats
    cands = "; ".join(
        f"{cid} ({(names.get(cid) or EntityStats()).name or '?'}"
        + (f", {score:.2f})" if score is not None else ")")
        for cid, score in m.candidates
    )
    return {
        "status": m.status,
        "entity_id": m.entity_id or "",
        "rebox_name": (s.name or "") if s else "",
        "facts": str(s.facts) if s else "",
        "edges": str(s.edges) if s else "",
        "sources": ";".join(s.sources) if s else "",
        "timeline": ("y" if s.timeline else "n") if s else "",
        "url": entity_url(m.entity_id, explorer_url) if m.entity_id else "",
        "candidates": cands,
    }


def output_fieldnames(input_fields: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """Input columns + report columns; a report column whose name the input already uses gets a
    ``coverage_`` prefix. Returns (fieldnames, report column -> output name)."""
    taken = {f.lower() for f in input_fields}
    rename = {c: (f"coverage_{c}" if c in taken else c) for c in OUTPUT_COLUMNS}
    return [*input_fields, *rename.values()], rename


def write_report(path: Path, input_fields: Sequence[str], cov: Coverage, explorer_url: str) -> None:
    fields, rename = output_fieldnames(input_fields)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for res in cov.results:
            rep = report_row(res, explorer_url, cov.entities)
            w.writerow({**res.row.raw, **{rename[k]: v for k, v in rep.items()}})


def summarize(results: Sequence[RowResult]) -> dict[str, Any]:
    by: dict[str, Counter[str]] = {"all": Counter(), **{k: Counter() for k in KINDS}}
    for res in results:
        by["all"][res.match.status] += 1
        by[res.row.kind][res.match.status] += 1
    totals = {k: sum(c.values()) for k, c in by.items()}
    sources: Counter[str] = Counter()
    for res in results:
        if res.known and res.stats:
            sources.update(res.stats.sources)
    known = [r for r in results if r.known]
    return {
        "rows": totals["all"],
        "by_kind": {k: totals[k] for k in KINDS},
        "status": {k: {s: {"n": by[k][s], "pct": _pct(by[k][s], totals[k])} for s in STATUSES} for k in by},
        "known": {"n": len(known), "pct": _pct(len(known), totals["all"])},
        "thin": [_brief(r) for r in results if r.thin],
        "no_timeline": sum(1 for r in known if r.stats and not r.stats.timeline),
        "top_sources": sources.most_common(10),
        "missing": [_brief(r) for r in results if r.match.status == "missing"],
        "ambiguous": [_brief(r) for r in results if r.match.status == "ambiguous"],
        "invalid_values": sum(len(r.row.invalid) for r in results),
    }


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _brief(r: RowResult) -> dict[str, Any]:
    out: dict[str, Any] = {"row": r.row.n, "kind": r.row.kind, "label": r.row.label}
    if r.match.entity_id:
        out["entity_id"] = r.match.entity_id
    if r.stats:
        out["facts"] = r.stats.facts
    if r.match.candidates:
        out["candidates"] = [c for c, _ in r.match.candidates]
    return out


def format_summary(summary: dict[str, Any], *, title: str) -> str:
    lines = [
        f"coverage: {title}: {summary['rows']} rows "
        f"({summary['by_kind']['person']} people, {summary['by_kind']['company']} companies)"
    ]
    lines.append(f"  {'status':<14}{'all':>14}{'person':>14}{'company':>14}")
    for s in STATUSES:
        cells = "".join(
            f"{summary['status'][k][s]['n']:>6} {summary['status'][k][s]['pct']:>5.1f}%  "
            for k in ("all", *KINDS)
        )
        lines.append(f"  {s:<14}{cells}".rstrip())
    lines.append(f"  known (matched + matched_name): {summary['known']['n']} ({summary['known']['pct']}%)")
    thin = summary["thin"]
    lines.append(
        f"  thin matches (<= {THIN_FACTS} facts): {len(thin)}"
        + (
            ": " + ", ".join(t["label"] for t in thin[:15]) + (" ..." if len(thin) > 15 else "")
            if thin
            else ""
        )
    )
    lines.append(f"  known without timeline events: {summary['no_timeline']}")
    if summary["top_sources"]:
        lines.append(
            "  top sources in matches: " + ", ".join(f"{src} {n}" for src, n in summary["top_sources"])
        )
    if summary["ambiguous"]:
        lines.append("  ambiguous: " + ", ".join(a["label"] for a in summary["ambiguous"][:20]))
    if summary["missing"]:
        miss = summary["missing"]
        lines.append(
            f"  missing ({len(miss)}): "
            + ", ".join(m["label"] for m in miss[:50])
            + (" ..." if len(miss) > 50 else "")
        )
    if summary["invalid_values"]:
        lines.append(f"  values that could not be normalized (ignored): {summary['invalid_values']}")
    return "\n".join(lines)


# ── enqueue ──────────────────────────────────────────────────────────────


def enqueue(db: Db, results: Sequence[RowResult], stem: str) -> dict[str, int]:
    """Missing and thin rows -> frontier (x_spider by handle, else exa by domain/name). Idempotent."""
    by = [f"coverage:{stem}"]
    x_items: list[frontier.Item] = []
    exa_items: list[frontier.Item] = []
    for res in results:
        if not (res.match.status == "missing" or res.thin):
            continue
        r = res.row
        payload = {
            k: v
            for k, v in {
                "via": "coverage", "file": stem, "row": r.n, "name": r.name, "company": r.company,
                "domain": r.domain, "entity_id": res.match.entity_id,
            }.items()
            if v is not None
        }  # fmt: skip
        if r.x_handle:
            x_items.append(
                frontier.Item(
                    key=f"@{r.x_handle}",
                    kind=r.kind,
                    label=r.x_handle,
                    discovered_by=by,
                    depth=0,
                    payload={**payload, "username": r.x_handle},
                )
            )
        elif r.domain or r.name:
            key = r.domain or (r.name or "").strip()
            exa_items.append(
                frontier.Item(
                    key=key, kind=r.kind, label=r.name or r.domain, discovered_by=by, depth=0, payload=payload
                )
            )
    with db.view() as conn:
        x_rows = frontier.upsert(conn, "x_spider", x_items)
        exa_rows = frontier.upsert(conn, "exa", exa_items)
    return {"x_spider": len(x_rows), "exa": len(exa_rows)}


# ── command ──────────────────────────────────────────────────────────────


def run(
    db: Db,
    path: Path,
    *,
    out: Path | None,
    as_json: bool,
    do_enqueue: bool,
    explorer_url: str,
    stream: Any = None,
) -> dict[str, Any]:
    stream = stream or sys.stdout
    t0 = time.monotonic()
    fieldnames, raw_rows = read_csv(path)
    with db.view() as conn:
        predicates = load_predicates(conn)
    rows = parse_rows(fieldnames, raw_rows, predicates)
    cov = check(db, rows)
    results = cov.results
    summary = summarize(results)
    if out is not None:
        write_report(out, fieldnames, cov, explorer_url)
        summary["report"] = str(out)
    if do_enqueue:
        summary["queued"] = enqueue(db, results, path.stem)
    summary["seconds"] = round(time.monotonic() - t0, 2)
    if as_json:
        rows_out = [{**res.row.raw, **report_row(res, explorer_url, cov.entities)} for res in results]
        print(json.dumps({"summary": summary, "rows": rows_out}, indent=1, default=str), file=stream)
    else:
        print(format_summary(summary, title=path.name), file=stream)
        if out is not None:
            print(f"  report: {out}", file=stream)
        if do_enqueue:
            q = summary["queued"]
            print(
                f"  queued: {q['x_spider']} for x_spider, {q['exa']} for exa "
                f"(discovered_by coverage:{path.stem}; nothing fetched)",
                file=stream,
            )
        print(f"  took {summary['seconds']}s", file=stream)
    return summary


def cmd_coverage(args: argparse.Namespace, settings: Any) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"rebox-worker: no such file {path}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else path.with_name(f"{path.stem}.report.csv")
    explorer = os.environ.get("EXPLORER_URL") or DEFAULT_EXPLORER_URL
    with Db(settings.database_url, max_size=2) as db:
        run(db, path, out=out, as_json=args.json, do_enqueue=args.enqueue, explorer_url=explorer)
    return 0


def add_parser(sub: Any) -> None:
    c = sub.add_parser("coverage", help="how much of a CSV list of people/companies Rebox knows")
    c.add_argument("file", help="CSV with a header (kind, name, x, github, domain, website, company, ...)")
    c.add_argument("--out", help="report CSV (default: <file stem>.report.csv next to the input)")
    c.add_argument("--json", action="store_true", help="print the summary and rows as JSON")
    c.add_argument("--enqueue", action="store_true", help="queue missing/thin rows in the frontier")
    c.set_defaults(func=cmd_coverage)
