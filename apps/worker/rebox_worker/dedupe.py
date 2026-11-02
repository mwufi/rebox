"""Dedupe pass: merge entities that are the same thing, queue the likely ones for review (S15).

``rebox-worker dedupe [--dry-run] [--kinds company,person] [--limit N] [--json]``

The resolver matches on hard keys, but a key can arrive *after* an entity was minted (Wikidata
adds ``domain: figma.com`` to an entity that an article minted by name), and the bio parser mints
companies it only knows by name once per batch. This pass cleans that up, in three phases. It
reads as ``rebox_app`` with all public layers visible, follows ``merged_into`` chains (an entity's
facts include those of everything merged into it) and never writes graph tables itself: every
merge is ``POST /v0/admin/merge {from, into}`` (ADMIN_TOKEN), so ``merge_log`` and the ``change``
feed are written by the one place that owns merges. Review items go to ``review_item``
(``kind='possible_duplicate'``), like the resolver's.

1. **Shared identity key.** Un-merged entities of the same kind that share a value of
   ``wikidata, domain, github, x_handle, x_id, hn_user`` (one indexed query per predicate,
   ``claim_value_text_idx``); groups that share members are unioned.

   * **Auto-merge** unless two members have *conflicting strong keys*: different ``wikidata`` or
     ``x_id`` values (any kind), or different ``github`` / ``x_handle`` values (persons). Several
     ``domain`` values are fine (companies have many).
   * Otherwise a review item (reason names the shared key, evidence lists the conflicts). Groups
     with a member an admin split from another member (``merge_log`` op ``split``), or that an
     admin dismissed as not duplicates, are never auto-merged either.
   * **Target:** the member with a ``wikidata`` claim, else the one with most claims, tie -> the
     oldest ``created_at`` (then the smallest id).

2. **Name-only company convergence** (companies only). A *name-only* company has no
   ``wikidata, domain, github, x_handle, x_id, hn_user, website, url`` claim at all. By folded
   name (``fold_name``; exact equality):

   * exactly one *keyed* company has that name and it is a *safe target* (``safe_target``: a
     ``wikidata`` claim, a ``domain`` whose registrable label is the name without spaces or
     punctuation, or >= 5 distinct referrers by non-follows edges) -> merge every name-only one
     into it; not safe (an X account named "Capital") -> review, reason
     ``name_only_unsafe_target``;
   * several keyed companies have it -> one review item listing them (no merge);
   * none -> merge the name-only companies among themselves into the one with most claims
     (tie: oldest), but only for a distinctive name (two words or 6+ characters); shorter ones
     ("ramp", "meta") are left alone, counted as ``name_only_skipped_short``.

   A name-only company is matched by its main names (``Profile.main_folds``), so one stray
   name never chains two companies together.

3. **Name + neighbor** (no shared key): same kind, equal folded name, and an edge (either
   direction, ``follows`` excluded; for people also places, a hub everyone is based_in) to the
   same entity -> review item, reason ``name+neighbor``. Never merged automatically.

A merge of exactly the two entities of an open ``ambiguous_match`` / ``possible_duplicate``
item passes its ``review_id``, so the API marks the item merged.

Review items are not reopened: an open or dismissed ``possible_duplicate`` item for the same set
of entities suppresses a new one. A second run finds nothing to merge (merged entities are no
longer roots). ``--limit N`` acts on at most N groups (merges or reviews); the rest wait.

The run is recorded as worker ``dedupe-enricher-<6 digits>`` (kind ``enricher``, run kind
``enrich``) with stats ``{groups, auto_merged, review_opened, skipped_conflict,
name_only_merged, name_only_unsafe, name_only_skipped_short, reviews_resolved, ...}``.
``--dry-run`` prints every group, the decision and the target, and writes nothing (no worker,
run, merge or review item).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TextIO

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .db import Db
from .resolve import _canonicalize

log = logging.getLogger(__name__)

IDENTITY_PREDICATES = ("wikidata", "domain", "github", "x_handle", "x_id", "hn_user")
# Any of these makes a company "keyed" (phase 2); the rest are only names.
KEY_PREDICATES = (*IDENTITY_PREDICATES, "website", "url")
# Two different values of one of these between two members = different things.
STRONG_KEYS: dict[str, tuple[str, ...]] = {"person": ("wikidata", "x_id", "github", "x_handle")}
DEFAULT_STRONG_KEYS = ("wikidata", "x_id")
DEFAULT_KINDS = ("company", "person")
REVIEW_KIND = "possible_duplicate"
LEGAL_FORMS = ("inc", "llc", "ltd", "corp", "corporation", "co", "gmbh", "sa")
MAX_DEPTH = 20
MIN_FOLDED_LEN = 2
# A keyed company is a safe target for name-only merges with this many distinct referrers (phase 2).
SAFE_MIN_REFERRERS = 5
# Name-only companies with no keyed match are merged among themselves only for distinctive names:
# at least two words or this many characters ("palantir" yes, "ramp" / "meta" / "base" no).
MIN_ORPHAN_NAME_LEN = 6
# Two-label public suffixes for ``registrable_label`` (the label left of them is the name).
TWO_PART_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
        "com.au", "net.au", "org.au", "co.nz", "org.nz", "co.jp", "ne.jp", "or.jp",
        "co.kr", "co.in", "com.br", "com.cn", "com.sg", "com.mx", "com.tr", "com.hk",
        "com.tw", "co.za", "co.il", "com.ar", "co.id", "com.my", "com.ph", "com.vn",
    }
)  # fmt: skip
OPEN_REVIEW_KINDS = ("ambiguous_match", REVIEW_KIND)  # resolved by a merge of exactly their pair
# Phase 3 neighbors that say nothing about a *person's* identity: half the people are based_in
# San Francisco. (Two same-named companies based in the same city are worth a look.)
PERSON_HUB_PREFIXES = ("rbx:place:",)

WORKER_NAME = "dedupe"
WORKER_KIND = "enricher"
WORKER_SOURCE = "dedupe"
WORKER_LAYERS = ("public/dedupe",)  # writes no claims; the registry requires a layer

# ── names ─────────────────────────────────────────────────────────────────

_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
_LEGAL_RE = re.compile(r"(?:^|[\s,]+)(?:" + "|".join(LEGAL_FORMS) + r")\.?$")


def fold_name(name: str, kind: str) -> str:
    """The name key for phases 2 and 3: lowercase, trimmed, without a trailing ``(...)``
    parenthetical, whitespace collapsed; companies also without trailing legal forms
    (``inc, llc, ltd, corp, corporation, co, gmbh, sa``, with or without a dot or comma).

    ``"Anysphere, Inc."`` -> ``"anysphere"``; ``"Notion (Notion Labs)"`` -> ``"notion"``.
    Never strips a name down to nothing (``"Co"`` stays ``"co"``).
    """
    s = " ".join(unicodedata.normalize("NFKC", name).casefold().split())
    stripped = _PAREN_RE.sub("", s).strip()
    s = stripped or s
    if kind == "company":
        while True:
            shorter = _LEGAL_RE.sub("", s).rstrip(" ,.")
            if not shorter or shorter == s:
                break
            s = shorter
    return s.strip(" ,")


def registrable_label(domain: str) -> str | None:
    """The label left of the public suffix: ``google.com`` -> ``google``, ``acme.co.uk`` ->
    ``acme``, ``app.notion.so`` -> ``notion`` (a simple rule: the label before the last dot, or
    before the last two when they are a known two-part suffix)."""
    labels = [x for x in domain.lower().strip(".").split(".") if x]
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_PART_SUFFIXES:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else None


def squash(folded: str) -> str:
    """A folded name without spaces or punctuation (``"scale ai"`` -> ``"scaleai"``)."""
    return "".join(ch for ch in folded if ch.isalnum())


def distinctive(folded: str) -> bool:
    """Distinctive enough to merge name-only companies on it alone (two words or 6+ characters)."""
    return len(folded.split()) >= 2 or len(folded) >= MIN_ORPHAN_NAME_LEN


# ── profiles ──────────────────────────────────────────────────────────────

# Roots (un-merged entities, by id or by kind) with everything merged into them, and their
# claims: the count of all claims, plus names and key values.
PROFILE_SQL = """
with recursive d(root, id, depth) as (
    select e.id, e.id, 0 from entity e
     where e.merged_into is null
       and (%(ids)s::text[] is null or e.id = any(%(ids)s::text[]))
       and (%(kind)s::text is null or e.kind = %(kind)s::text)
  union all
    select d.root, e.id, d.depth + 1 from d join entity e on e.merged_into = d.id
     where d.depth < %(depth)s
)
select d.root as id, r.kind, r.created_at,
       count(c.id) as claims,
       coalesce(jsonb_agg(jsonb_build_array(c.predicate, c.value_text))
                  filter (where c.object_id is null and c.value_text is not null
                            and c.predicate = any(%(keep)s::text[])), '[]'::jsonb) as vals
  from d
  join entity r on r.id = d.root
  left join claim c on c.subject = d.id and not c.retract
 group by d.root, r.kind, r.created_at
"""

# Phase 1: identity values shared by more than one subject (one probe per predicate on
# claim_value_text_idx, which is usable under RLS; CONTRACTS §2a).
GROUP_SQL = """
select c.value_text as value, array_agg(distinct c.subject) as subjects
  from claim c
 where c.predicate = %(p)s and c.object_id is null and not c.retract and c.value_text is not null
 group by c.value_text
having count(distinct c.subject) > 1
"""

# Phase 3: the other ends of every non-follows edge of the roots (and what is merged into them).
NEIGHBOR_SQL = """
with recursive d(root, id, depth) as (
    select e.id, e.id, 0 from entity e where e.id = any(%(ids)s::text[])
  union all
    select d.root, e.id, d.depth + 1 from d join entity e on e.merged_into = d.id
     where d.depth < %(depth)s
)
select d.root, c.object_id as other from d
  join claim c on c.subject = d.id
 where c.object_id is not null and not c.retract and c.predicate <> 'follows'
union
select d.root, c.subject as other from d
  join claim c on c.object_id = d.id
 where not c.retract and c.predicate <> 'follows'
"""


# Phase 2: distinct entities with a non-follows edge into each candidate target (and into what
# is merged into it).
REFERRERS_SQL = """
with recursive d(root, id, depth) as (
    select e.id, e.id, 0 from entity e where e.id = any(%(ids)s::text[])
  union all
    select d.root, e.id, d.depth + 1 from d join entity e on e.merged_into = d.id
     where d.depth < %(depth)s
)
select distinct d.root, c.subject from d
  join claim c on c.object_id = d.id
 where not c.retract and c.predicate <> 'follows' and c.subject <> d.id
"""


@dataclass
class Profile:
    id: str
    kind: str
    created_at: datetime
    claims: int = 0
    names: dict[str, int] = field(default_factory=dict)  # name -> number of name claims
    keys: dict[str, set[str]] = field(default_factory=dict)

    @property
    def keyed(self) -> bool:
        return any(self.keys.get(p) for p in KEY_PREDICATES)

    @property
    def display(self) -> str:
        return min(self.names, key=lambda n: (len(n), n)) if self.names else ""

    def folds(self) -> set[str]:
        return {f for f in (fold_name(n, self.kind) for n in self.names) if len(f) >= MIN_FOLDED_LEN}

    def _fold_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for n, c in self.names.items():
            f = fold_name(n, self.kind)
            if len(f) >= MIN_FOLDED_LEN:
                counts[f] += c
        return counts

    def primary_fold(self) -> str | None:
        """The folded name most of its name claims agree on (tie: shortest, then alphabetical)."""
        counts = self._fold_counts()
        return min(counts, key=lambda f: (-counts[f], len(f), f)) if counts else None

    def main_folds(self) -> set[str]:
        """The primary folded name plus any other with at least a third of the name claims.

        A parser sometimes gives one entity a stray second name ("Ramp" also once named
        "Glass"); matching name-only companies on every name would chain unrelated companies
        together, matching on the primary one only would miss "Notion" named "NotionHQ" 7 of 12
        times."""
        counts = self._fold_counts()
        if not counts:
            return set()
        total = sum(counts.values())
        return {self.primary_fold() or ""} | {f for f, c in counts.items() if 3 * c >= total}

    def absorb(self, other: Profile) -> None:
        """What a merge does to the reads: the target shows the merged entity's facts too."""
        self.claims += other.claims
        for n, c in other.names.items():
            self.names[n] = self.names.get(n, 0) + c
        for p, vs in other.keys.items():
            self.keys.setdefault(p, set()).update(vs)

    def brief(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.display, "claims": self.claims}


def load_profiles(
    conn: psycopg.Connection, *, ids: Iterable[str] | None = None, kind: str | None = None
) -> dict[str, Profile]:
    rows = conn.execute(
        PROFILE_SQL,
        {
            "ids": sorted(set(ids)) if ids is not None else None,
            "kind": kind,
            "depth": MAX_DEPTH,
            "keep": ["name", *KEY_PREDICATES],
        },
    ).fetchall()
    out: dict[str, Profile] = {}
    for r in rows:
        p = Profile(r["id"], r["kind"], r["created_at"], int(r["claims"]))
        for pred, value in r["vals"]:
            if pred == "name":
                p.names[value] = p.names.get(value, 0) + 1
            else:
                p.keys.setdefault(pred, set()).add(value)
        out[p.id] = p
    return out


def target_key(p: Profile) -> tuple[Any, ...]:
    """Sort key: the canonical target first (wikidata, most claims, oldest, smallest id)."""
    return (0 if p.keys.get("wikidata") else 1, -p.claims, p.created_at, p.id)


def strong_conflicts(kind: str, members: Sequence[Profile]) -> list[dict[str, Any]]:
    """Strong keys on which two members disagree (both have the key, and the pair has two values)."""
    out = []
    for pred in STRONG_KEYS.get(kind, DEFAULT_STRONG_KEYS):
        having = [m for m in members if m.keys.get(pred)]
        clash = any(
            len(a.keys[pred] | b.keys[pred]) > 1 for i, a in enumerate(having) for b in having[i + 1 :]
        )
        if clash:
            out.append({"predicate": pred, "values": {m.id: sorted(m.keys[pred]) for m in having}})
    return out


def safe_target(target: Profile, folded: Iterable[str], referrers: int) -> str | None:
    """Why a keyed company is a safe target for name-only companies named ``folded``, or None.

    Safe when it has a ``wikidata`` claim, or a ``domain`` whose registrable label is the name
    without spaces or punctuation (``ramp.com`` for "ramp"), or at least ``SAFE_MIN_REFERRERS``
    distinct entities with a non-follows edge into it."""
    if target.keys.get("wikidata"):
        return "wikidata"
    names = {squash(f) for f in folded} - {""}
    for dom in sorted(target.keys.get("domain", ())):
        if registrable_label(dom) in names:
            return f"domain {dom}"
    if referrers >= SAFE_MIN_REFERRERS:
        return f"{referrers} referrers"
    return None


# ── admin API ─────────────────────────────────────────────────────────────


class MergeError(RuntimeError):
    pass


class AdminClient:
    """``POST /v0/admin/merge`` with the admin token. Retries network errors, 429 and 5xx."""

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        user_agent: str = "rebox-bot/0.1",
        max_retries: int = 3,
        backoff_s: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent},
            timeout=60.0,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.sleep = sleep

    def merge(self, from_id: str, into_id: str, *, review_id: int | None = None) -> dict[str, Any]:
        """Merge ``from_id`` into ``into_id``; ``review_id`` also resolves that review item."""
        body: dict[str, Any] = {"from": from_id, "into": into_id}
        if review_id is not None:
            body["review_id"] = review_id
        error = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.http.post("/v0/admin/merge", json=body)
            except httpx.TransportError as e:
                error = f"network error: {e!r}"
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code < 500 and resp.status_code != 429:
                    raise MergeError(f"merge {from_id} -> {into_id}: HTTP {resp.status_code}: {_msg(resp)}")
                error = f"HTTP {resp.status_code}"
            if attempt < self.max_retries:
                self.sleep(self.backoff_s * 2**attempt)
        raise MergeError(
            f"merge {from_id} -> {into_id}: giving up after {self.max_retries + 1} tries: {error}"
        )

    def close(self) -> None:
        self.http.close()


def _msg(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        details = data.get("details") or []
        if details:
            return "; ".join(str(d.get("message")) for d in details if isinstance(d, dict))[:300]
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]


# ── decisions ─────────────────────────────────────────────────────────────


@dataclass
class Decision:
    phase: str  # 'key' | 'name_only' | 'name_neighbor'
    kind: str
    action: str  # 'merge' | 'review'
    members: list[Profile]
    reason: str
    target: Profile | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    result: str = ""  # merged / review_opened / review_exists / deferred / planned / error...
    merged: int = 0
    errors: list[str] = field(default_factory=list)
    resolved: list[int] = field(default_factory=list)  # review items the merges resolved

    @property
    def ids(self) -> list[str]:
        return sorted(m.id for m in self.members)

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "kind": self.kind,
            "action": self.action,
            "target": self.target.brief() if self.target else None,
            "members": [m.brief() for m in sorted(self.members, key=target_key)],
            "reason": self.reason,
            "evidence": self.evidence,
            "result": self.result,
            **({"errors": self.errors} if self.errors else {}),
        }

    def line(self) -> str:
        def ref(p: Profile) -> str:
            return f'{p.id} "{p.display}" ({p.claims})'

        if self.action == "merge" and self.target is not None:
            others = [m for m in sorted(self.members, key=target_key) if m.id != self.target.id]
            shown = ", ".join(ref(m) for m in others[:5]) + (
                f", +{len(others) - 5} more" if len(others) > 5 else ""
            )
            head = f"merge into {ref(self.target)} <- {shown}"
        else:
            head = "review " + ", ".join(ref(m) for m in sorted(self.members, key=target_key)[:6])
        return f"[{self.phase}] {self.kind} {head} | {self.reason} => {self.result}"


@dataclass
class Report:
    dry_run: bool
    kinds: tuple[str, ...]
    groups_by_predicate: dict[str, dict[str, int]] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    worker: str | None = None
    run_id: int | None = None
    skipped_short: list[tuple[str, int]] = field(default_factory=list)  # (folded name, companies)
    stats: dict[str, int] = field(
        default_factory=lambda: {
            "groups": 0,
            "auto_merged": 0,
            "review_opened": 0,
            "skipped_conflict": 0,
            "name_only_merged": 0,
            "name_only_groups": 0,
            "name_only_unsafe": 0,
            "name_only_skipped_short": 0,
            "reviews_resolved": 0,
            "name_neighbor_groups": 0,
            "review_existing": 0,
            "merge_errors": 0,
            "deferred": 0,
        }
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "kinds": list(self.kinds),
            "groups_by_predicate": self.groups_by_predicate,
            "stats": self.stats,
            **({"worker": self.worker, "run_id": self.run_id} if self.worker else {}),
            "name_only_skipped_short": [{"folded": f, "companies": n} for f, n in self.skipped_short],
            "decisions": [d.as_dict() for d in self.decisions],
        }


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            out[self.find(x)].append(x)
        return [sorted(v) for v in out.values() if len(v) > 1]


# ── the pass ──────────────────────────────────────────────────────────────


class Dedupe:
    """One dedupe pass. ``merger`` is an ``AdminClient`` (anything with ``merge(from, into)``);
    with ``dry_run`` it is never called and nothing is written."""

    def __init__(
        self,
        db: Db,
        *,
        merger: Any = None,
        kinds: Sequence[str] = DEFAULT_KINDS,
        dry_run: bool = False,
        limit: int | None = None,
        out: TextIO | None = None,
    ) -> None:
        if not dry_run and merger is None:
            raise ValueError("a merger (admin API client) is required unless dry_run")
        self.db = db
        self.merger = merger
        self.kinds = tuple(dict.fromkeys(kinds))
        self.dry_run = dry_run
        self.limit = limit
        self.out = out
        self.report = Report(dry_run=dry_run, kinds=self.kinds)
        self.planned: dict[str, str] = {}  # from -> into, merged (or planned) in this run
        self._acted = 0
        self._splits: list[tuple[str, str]] | None = None
        self._dismissed: list[tuple[int, list[str]]] = []
        self._reviewed: set[tuple[str, ...]] = set()
        self._open_pairs: dict[frozenset[str], int] | None = (
            None  # open review items of two ids  # review sets opened (or planned) in this run
        )

    # ── driver ──

    def run(self) -> Report:
        self._key_phase()
        if "company" in self.kinds:
            self._name_only_phase()
        self._name_neighbor_phase()
        return self.report

    # ── helpers ──

    def _root(self, conn: psycopg.Connection, ids: Iterable[str]) -> dict[str, str]:
        """id -> its root now: the end of its merged_into chain, then of this run's merges."""
        canon = _canonicalize(conn, ids)
        return {start: self._follow(cand.id) for start, cand in canon.items()}

    def _profiles(self, conn: psycopg.Connection, *, ids=None, kind=None) -> dict[str, Profile]:
        """Root profiles, with this run's planned merges applied (they are already in the DB
        unless this is a dry run)."""
        profs = load_profiles(conn, ids=ids, kind=kind)
        if not self.planned:
            return profs
        root = self._root(conn, list(profs))
        for pid in list(profs):
            r = root.get(pid, pid)
            if r != pid:
                p = profs.pop(pid)
                if r in profs:
                    profs[r].absorb(p)
                elif ids is not None:  # the target was not asked for; load it
                    extra = load_profiles(conn, ids=[r])
                    if r in extra:
                        extra[r].absorb(p)
                        profs[r] = extra[r]
        return profs

    def _admin_decisions(self, conn: psycopg.Connection) -> None:
        """Splits (merge_log) and dismissed possible_duplicate items, loaded once per run."""
        if self._splits is None:
            self._splits = [
                (r["from_id"], r["into_id"])
                for r in conn.execute(
                    "select from_id, into_id from merge_log where op = 'split' and into_id is not null"
                ).fetchall()
            ]
            self._dismissed = [
                (int(r["id"]), list(r["entity_ids"]))
                for r in conn.execute(
                    "select id, entity_ids from review_item where kind = %s and status = 'dismissed'",
                    (REVIEW_KIND,),
                ).fetchall()
            ]

    def _blocked(self, conn: psycopg.Connection, members: Sequence[str]) -> list[dict[str, Any]]:
        """Admin decisions against merging members: a split between two of them (their roots now),
        or a dismissed possible_duplicate item naming two of them. Such groups are reviewed, never
        merged automatically, so an undo (``POST /v0/admin/split``) sticks."""
        self._admin_decisions(conn)
        ms = set(members)
        ids = {i for pair in self._splits for i in pair} | {i for _, es in self._dismissed for i in es}
        if not ids:
            return []
        root = self._root(conn, ids)
        out: list[dict[str, Any]] = []
        for a, b in self._splits:
            ra, rb = root.get(a), root.get(b)
            if ra in ms and rb in ms and ra != rb:
                out.append({"split": [a, b]})
        for rid, es in self._dismissed:
            if len({root.get(i) for i in es} & ms) > 1:
                out.append({"dismissed_review": rid})
        return out

    def _open_review_pairs(self) -> dict[frozenset[str], int]:
        """Open ambiguous_match / possible_duplicate items naming exactly two entities, by pair:
        a merge of exactly that pair passes the item's ``review_id`` so the API resolves it."""
        if self._open_pairs is None:
            with self.db.view() as conn:
                rows = conn.execute(
                    """select id, entity_ids from review_item
                        where status = 'open' and kind = any(%s) and cardinality(entity_ids) = 2
                        order by id""",
                    (list(OPEN_REVIEW_KINDS),),
                ).fetchall()
            self._open_pairs = {}
            for r in rows:
                self._open_pairs.setdefault(frozenset(r["entity_ids"]), int(r["id"]))
        return self._open_pairs

    def _referrers(self, conn: psycopg.Connection, ids: Iterable[str]) -> dict[str, int]:
        """Root -> number of distinct entities with a non-follows edge into it (or into what is
        merged into it, also by this run's planned merges)."""
        roots = set(ids)
        if not roots:
            return {}
        query = roots | {c for c in self.planned if self._follow(c) in roots}
        rows = conn.execute(REFERRERS_SQL, {"ids": sorted(query), "depth": MAX_DEPTH}).fetchall()
        subjects: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            subjects[self._follow(r["root"])].add(r["subject"])
        return {r: len(subjects[r] - {r}) for r in roots}

    def _follow(self, x: str) -> str:
        seen: set[str] = set()
        while x in self.planned and x not in seen:
            seen.add(x)
            x = self.planned[x]
        return x

    def _take(self) -> bool:
        """One more group may be acted on (``--limit``)."""
        if self.limit is not None and self._acted >= self.limit:
            return False
        self._acted += 1
        return True

    def _emit(self, d: Decision) -> None:
        self.report.decisions.append(d)
        if self.out is not None:
            print(d.line(), file=self.out)

    def _review(self, conn: psycopg.Connection, d: Decision) -> None:
        ids = d.ids
        params = {"kind": REVIEW_KIND, "ids": ids, "reason": d.reason, "evidence": d.evidence}
        exists = (
            tuple(ids) in self._reviewed
            or conn.execute(
                """select 1 from review_item where kind = %(kind)s and entity_ids = %(ids)s::text[]
                 and status in ('open', 'dismissed') limit 1""",
                params,
            ).fetchone()
        )
        if exists:  # e.g. a conflicting key group that phase 3 finds again by name
            d.result = "review_exists"
            self.report.stats["review_existing"] += 1
            return
        if not self._take():
            d.result = "deferred"
            self.report.stats["deferred"] += 1
            return
        self._reviewed.add(tuple(ids))
        if self.dry_run:
            d.result = "would_open_review"
            self.report.stats["review_opened"] += 1
            return
        conn.execute(
            """insert into review_item (kind, entity_ids, reason, evidence)
               values (%(kind)s, %(ids)s::text[], %(reason)s, %(evidence)s)""",
            {**params, "evidence": Jsonb(d.evidence)},
        )
        d.result = "review_opened"
        self.report.stats["review_opened"] += 1

    def _merge(self, d: Decision, stat: str) -> None:
        assert d.target is not None
        if not self._take():
            d.result = "deferred"
            self.report.stats["deferred"] += 1
            return
        pairs = self._open_review_pairs()
        for m in sorted(d.members, key=target_key):
            if m.id == d.target.id:
                continue
            rid = pairs.pop(frozenset((m.id, d.target.id)), None)
            if self.dry_run:
                self.planned[m.id] = d.target.id
                d.merged += 1
                if rid is not None:
                    d.resolved.append(rid)
                continue
            try:
                try:
                    self.merger.merge(m.id, d.target.id, review_id=rid)
                except MergeError as e:
                    if rid is None or "review item" not in str(e):
                        raise
                    rid = None  # resolved meanwhile: merge without it
                    self.merger.merge(m.id, d.target.id, review_id=None)
            except MergeError as e:
                log.warning("dedupe: %s", e)
                d.errors.append(str(e))
                self.report.stats["merge_errors"] += 1
                continue
            self.planned[m.id] = d.target.id
            d.merged += 1
            if rid is not None:
                d.resolved.append(rid)
        self.report.stats["reviews_resolved"] += len(d.resolved)
        self.report.stats[stat] += d.merged
        d.result = ("would_merge" if self.dry_run else "merged") + f" {d.merged}"
        if d.resolved:
            d.result += f", resolves review {', '.join(map(str, d.resolved))}"
        if d.errors:
            d.result += f", {len(d.errors)} failed"

    # ── phase 1: shared identity keys ──

    def _key_phase(self) -> None:
        with self.db.view() as conn:
            shared: dict[tuple[str, str], list[str]] = {}
            for pred in IDENTITY_PREDICATES:
                for r in conn.execute(GROUP_SQL, {"p": pred}).fetchall():
                    shared[(pred, r["value"])] = r["subjects"]
            canon = _canonicalize(conn, {s for ss in shared.values() for s in ss})
            uf: dict[str, _UnionFind] = defaultdict(_UnionFind)
            keys_of: dict[str, list[tuple[str, str]]] = defaultdict(list)  # root -> shared keys
            for pred in IDENTITY_PREDICATES:
                self.report.groups_by_predicate[pred] = {k: 0 for k in self.kinds}
            for (pred, value), subjects in shared.items():
                by_kind: dict[str, set[str]] = defaultdict(set)
                for s in subjects:
                    c = canon.get(s)
                    if c is not None and c.kind in self.kinds:
                        by_kind[c.kind].add(c.id)
                for kind, roots in by_kind.items():
                    if len(roots) < 2:
                        continue
                    self.report.groups_by_predicate[pred][kind] += 1
                    rs = sorted(roots)
                    for r in rs:
                        uf[kind].union(rs[0], r)
                        keys_of[r].append((pred, value))
            comps = [(kind, g) for kind in self.kinds for g in uf[kind].groups()]
            self.report.stats["groups"] = len(comps)
            profs = load_profiles(conn, ids={i for _, g in comps for i in g}) if comps else {}

        for kind, group in comps:
            members = [profs[i] for i in group if i in profs]
            if len(members) < 2:
                continue
            keys = sorted({k for i in group for k in keys_of[i]})
            shared_txt = ", ".join(f"{p}={v}" for p, v in keys[:4]) + (" ..." if len(keys) > 4 else "")
            with self.db.view() as conn:
                conflicts = strong_conflicts(kind, members)
                blocked = self._blocked(conn, group)
                evidence: list[dict[str, Any]] = [{"shared": [{"predicate": p, "value": v} for p, v in keys]}]
                if conflicts or blocked:
                    self.report.stats["skipped_conflict"] += 1
                    why = "; ".join(
                        [f"conflicting {c['predicate']}" for c in conflicts]
                        + ["split by an admin" for b in blocked if "split" in b][:1]
                        + ["dismissed by an admin" for b in blocked if "dismissed_review" in b][:1]
                    )
                    d = Decision(
                        "key",
                        kind,
                        "review",
                        members,
                        f"share {shared_txt}; {why}",
                        evidence=[*evidence, *({"conflict": c} for c in conflicts), *blocked],
                    )
                    self._review(conn, d)
                else:
                    target = min(members, key=target_key)
                    d = Decision("key", kind, "merge", members, f"share {shared_txt}", target, evidence)
            if d.action == "merge":
                self._merge(d, "auto_merged")
            self._emit(d)

    # ── phase 2: name-only companies ──

    def _name_only_phase(self) -> None:
        kind = "company"
        with self.db.view() as conn:
            profs = self._profiles(conn, kind=kind)
        keyed: dict[str, set[str]] = defaultdict(set)
        name_only: list[Profile] = []
        for p in profs.values():
            if p.keyed:
                for f in p.folds():
                    keyed[f].add(p.id)
            elif p.primary_fold():
                name_only.append(p)

        into: dict[str, list[Profile]] = defaultdict(list)  # the one keyed match -> name-only
        ambiguous: dict[tuple[str, ...], list[Profile]] = defaultdict(list)  # several keyed matches
        orphans: dict[str, list[Profile]] = defaultdict(list)  # no keyed match, by primary name
        for n in name_only:
            hits = sorted(set().union(*(keyed.get(f, set()) for f in n.main_folds())))
            if len(hits) == 1:
                into[hits[0]].append(n)
            elif hits:
                ambiguous[tuple(hits)].append(n)
            else:
                orphans[n.primary_fold() or ""].append(n)

        def label(ns: Sequence[Profile], within: set[str] | None = None) -> list[str]:
            fs = {f for n in ns for f in n.main_folds()}
            return sorted(fs & within if within is not None else fs)

        with self.db.view() as conn:
            referrers = self._referrers(conn, into)
        decisions: list[Decision] = []
        for tid, ns in sorted(into.items()):
            t, fs = profs[tid], label(ns, profs[tid].folds())
            why = safe_target(t, fs, referrers.get(tid, 0))
            ev = {
                "folded": fs,
                "name_only": sorted(n.id for n in ns),
                "safe": why,
                "referrers": referrers.get(tid, 0),
            }
            if why:
                d = Decision(
                    "name_only",
                    kind,
                    "merge",
                    [t, *ns],
                    f"name-only {', '.join(map(repr, fs))} -> the one keyed company ({why})",
                    t,
                )
            else:
                # One keyed company has the name, but nothing says it is *the* company of that name
                # (an X account called "Capital"): ask instead of merging.
                self.report.stats["name_only_unsafe"] += 1
                d = Decision(
                    "name_only",
                    kind,
                    "review",
                    [t, *ns],
                    f"name_only_unsafe_target: name-only {', '.join(map(repr, fs))} -> {t.display!r}"
                    f" (no wikidata, no matching domain, {referrers.get(tid, 0)} referrers)",
                )
            d.evidence = [ev]
            decisions.append(d)
        for ids, ns in sorted(ambiguous.items()):
            fs = label(ns)
            d = Decision(
                "name_only",
                kind,
                "review",
                [profs[i] for i in ids],
                f"name-only {', '.join(map(repr, fs))}: {len(ids)} keyed companies have that name",
            )
            d.evidence = [{"folded": fs, "name_only": sorted(n.id for n in ns)}]
            decisions.append(d)
        for f, ns in sorted(orphans.items()):
            if len(ns) < 2:
                continue
            if not distinctive(f):  # "ramp", "meta", "base": too generic to merge on, too noisy to review
                self.report.stats["name_only_skipped_short"] += 1
                self.report.skipped_short.append((f, len(ns)))
                continue
            t = min(ns, key=lambda p: (-p.claims, p.created_at, p.id))
            d = Decision("name_only", kind, "merge", ns, f"name-only {f!r}, no keyed company has it", t)
            d.evidence = [{"folded": [f], "name_only": sorted(n.id for n in ns)}]
            decisions.append(d)
        self.report.stats["name_only_groups"] = len(decisions)
        for d in decisions:
            with self.db.view() as conn:
                if d.action == "merge":
                    blocked = self._blocked(conn, [m.id for m in d.members])
                    if blocked:
                        d.action = "review"
                        d.target = None
                        d.reason += "; split/dismissed by an admin"
                        d.evidence += blocked
                if d.action == "review":
                    self._review(conn, d)
            if d.action == "merge":
                self._merge(d, "name_only_merged")
            self._emit(d)

    # ── phase 3: same name + a shared neighbor ──

    def _name_neighbor_phase(self) -> None:
        for kind in self.kinds:
            with self.db.view() as conn:
                profs = self._profiles(conn, kind=kind)
                by_name: dict[str, set[str]] = defaultdict(set)
                for p in profs.values():
                    if kind == "company" and not p.keyed:
                        continue  # name-only companies are phase 2's
                    for f in p.folds():
                        by_name[f].add(p.id)
                cands = {f: ids for f, ids in by_name.items() if len(ids) > 1}
                if not cands:
                    continue
                ids = {i for v in cands.values() for i in v}
                ids |= {c for c in self.planned if self._follow(c) in ids}  # dry run: planned merges
                rows = conn.execute(NEIGHBOR_SQL, {"ids": sorted(ids), "depth": MAX_DEPTH}).fetchall()
                root = self._root(conn, {r["other"] for r in rows})
            nbrs: dict[str, set[str]] = defaultdict(set)
            for r in rows:
                me, o = self._follow(r["root"]), root.get(r["other"])
                hub = kind == "person" and o is not None and o.startswith(PERSON_HUB_PREFIXES)
                if o is not None and o != me and not hub:
                    nbrs[me].add(o)
            uf = _UnionFind()
            shared_by: dict[frozenset[str], set[str]] = {}
            for _f, group in sorted(cands.items()):
                g = sorted(group)
                for i, a in enumerate(g):
                    for b in g[i + 1 :]:
                        common = (nbrs[a] & nbrs[b]) - {a, b}
                        if common:
                            uf.union(a, b)
                            shared_by[frozenset((a, b))] = common
            for comp in uf.groups():
                members = [profs[i] for i in comp]
                common = sorted(set().union(*(v for k, v in shared_by.items() if k <= set(comp))))
                folded = sorted(set.intersection(*(m.folds() for m in members)) or members[0].folds())
                conflicts = strong_conflicts(kind, members)
                d = Decision(
                    "name_neighbor",
                    kind,
                    "review",
                    members,
                    f"name+neighbor: {', '.join(repr(f) for f in folded)}; {len(common)} shared neighbor(s)"
                    + "".join(f"; conflicting {c['predicate']}" for c in conflicts),
                    evidence=[
                        {"folded": folded, "shared_neighbors": common[:20]},
                        *({"conflict": c} for c in conflicts),
                    ],
                )
                self.report.stats["name_neighbor_groups"] += 1
                with self.db.view() as conn:
                    self._review(conn, d)
                self._emit(d)


# ── command ───────────────────────────────────────────────────────────────


def run_dedupe(
    db: Db,
    *,
    client: Any = None,
    merger: Any = None,
    kinds: Sequence[str] = DEFAULT_KINDS,
    dry_run: bool = False,
    limit: int | None = None,
    out: TextIO | None = None,
) -> Report:
    """A recorded pass: registers the dedupe worker and opens/closes an ``enrich`` run (with the
    writer ``client``); a dry run records nothing."""
    dd = Dedupe(db, merger=merger, kinds=kinds, dry_run=dry_run, limit=limit, out=out)
    if dry_run:
        return dd.run()
    from .identity import WorkerSpec, register

    worker = register(
        client,
        WorkerSpec(
            name=WORKER_NAME,
            kind=WORKER_KIND,
            source=WORKER_SOURCE,
            layers=WORKER_LAYERS,
            config={"kinds": list(dd.kinds)},
            description="Dedupe pass: merges entities that share an identity key (via the admin API), "
            "converges name-only companies, and queues likely duplicates for review",
        ),
    )
    run_id = client.open_run(worker, "enrich")
    log.info("dedupe: running as %s (run %d)", worker, run_id)
    try:
        report = dd.run()
    except BaseException as e:
        try:
            client.update_run(
                run_id, status="failed", stats=dd.report.stats, error=f"{type(e).__name__}: {e}"
            )
        except Exception as e2:  # noqa: BLE001
            log.error("dedupe: could not close run %d: %s", run_id, e2)
        raise
    client.update_run(run_id, status="ok", stats=report.stats)
    report.worker, report.run_id = worker, run_id
    return report


def format_summary(report: Report) -> str:
    lines = ["key groups by predicate (before union):"]
    for pred, by_kind in report.groups_by_predicate.items():
        lines.append(f"  {pred:<9} " + ", ".join(f"{k} {n}" for k, n in by_kind.items()))
    acts: dict[tuple[str, str], int] = defaultdict(int)
    for d in report.decisions:
        acts[(d.phase, d.action)] += 1
    lines.append("decisions: " + ", ".join(f"{p}/{a} {n}" for (p, a), n in sorted(acts.items())))
    if report.skipped_short:
        shown = ", ".join(f"{f!r} x{n}" for f, n in report.skipped_short[:30])
        more = f" (+{len(report.skipped_short) - 30} more)" if len(report.skipped_short) > 30 else ""
        lines.append(
            f"name-only groups left alone (name too short): {len(report.skipped_short)}: {shown}{more}"
        )
    lines.append(("would: " if report.dry_run else "stats: ") + json.dumps(report.stats, sort_keys=True))
    return "\n".join(lines)


def cmd_dedupe(args: argparse.Namespace, settings: Any) -> int:
    from .client import ClaimsClient
    from .config import ConfigError

    kinds = [k.strip() for k in (args.kinds or "").split(",") if k.strip()] or list(DEFAULT_KINDS)
    out = None if args.json else sys.stdout
    with Db(settings.database_url, max_size=2) as db:
        if args.dry_run:
            print(f"dedupe (dry run; nothing is written): kinds {','.join(kinds)}", file=sys.stdout)
            report = run_dedupe(db, kinds=kinds, dry_run=True, limit=args.limit, out=out)
        else:
            api_url, token = settings.require_writer()
            admin = (_env(settings).get("ADMIN_TOKEN") or "").strip()
            if not admin:
                raise ConfigError("ADMIN_TOKEN is required to merge (or use --dry-run)")
            client = ClaimsClient(api_url, token, user_agent=settings.user_agent)
            merger = AdminClient(api_url, admin, user_agent=settings.user_agent)
            try:
                report = run_dedupe(db, client=client, merger=merger, kinds=kinds, limit=args.limit, out=out)
            finally:
                client.close()
                merger.close()
    if args.json:
        print(json.dumps(report.as_dict(), indent=1, default=str))
    else:
        print(format_summary(report))
    return 0


def _env(settings: Any) -> Mapping[str, str]:
    return getattr(settings, "extra", None) or {}


def add_parser(sub: Any) -> None:
    d = sub.add_parser("dedupe", help="merge entities sharing an identity key; queue likely duplicates")
    d.add_argument("--dry-run", action="store_true", help="print groups and decisions; write nothing")
    d.add_argument("--kinds", default=",".join(DEFAULT_KINDS), help="entity kinds (default company,person)")
    d.add_argument("--limit", type=int, help="act on at most N groups (merges or reviews)")
    d.add_argument("--json", action="store_true", help="print the full report as JSON")
    d.set_defaults(func=cmd_dedupe)
