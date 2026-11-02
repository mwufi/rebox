"""Discovery frontier (CONTRACTS §13) and the spiders' seed configuration (``config/seeds.yaml``).

The frontier is operational state, not graph facts: what a spider has discovered and might
index next. Rows are keyed by ``(source, key)``; ``discovered_by`` is the set of seeds (or
queries) that surfaced the row and ``score`` is its size, so an X account followed by three
seed accounts scores 3. ``depth`` keeps the smallest depth seen. ``status`` is never moved
backwards by an upsert (a crawled account stays ``fetched``).

The worker reaches the table through its own pool as ``rebox_app`` (``ctx.db.view()``); the
table has no RLS.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

# ── frontier rows ─────────────────────────────────────────────────────────


@dataclass
class Item:
    key: str
    kind: str
    label: str | None = None
    discovered_by: Sequence[str] = ()
    depth: int = 0
    payload: Any = None
    status: str | None = None  # None = keep the row's status ('new' for a new row)


UPSERT_SQL = """
insert into frontier as f (source, key, kind, label, score, depth, discovered_by, status, payload,
                           fetched_at)
select %(source)s, i.key, i.kind, i.label,
       cardinality(i.by), i.depth, i.by, coalesce(i.status, 'new'), i.payload,
       case when i.status = 'fetched' then now() end
  from jsonb_to_recordset(%(items)s) as i(key text, kind text, label text, depth int,
                                          by text[], status text, payload jsonb)
on conflict (source, key) do update set
  kind          = excluded.kind,
  label         = coalesce(excluded.label, f.label),
  depth         = least(f.depth, excluded.depth),
  discovered_by = array(select distinct d from unnest(f.discovered_by || excluded.discovered_by) d
                         order by d),
  score         = (select count(distinct d) from unnest(f.discovered_by || excluded.discovered_by) d),
  status        = case when excluded.status = 'new' and f.status <> 'new' then f.status
                       else excluded.status end,
  payload       = coalesce(excluded.payload, f.payload),
  last_seen_at  = now(),
  fetched_at    = case when excluded.status = 'fetched' then now() else f.fetched_at end
returning key, kind, label, score, depth, discovered_by, status
"""


def upsert(conn: psycopg.Connection, source: str, items: Iterable[Item]) -> dict[str, dict[str, Any]]:
    """Insert or merge rows; returns key -> the row after the merge (score, discovered_by, ...)."""
    merged: dict[str, Item] = {}
    for it in items:  # one statement may not touch a row twice: merge duplicates first
        prev = merged.get(it.key)
        if prev is None:
            merged[it.key] = Item(**{**it.__dict__, "discovered_by": list(dict.fromkeys(it.discovered_by))})
        else:
            prev.discovered_by = list(dict.fromkeys([*prev.discovered_by, *it.discovered_by]))
            prev.depth = min(prev.depth, it.depth)
            prev.kind, prev.label = it.kind, it.label or prev.label
            prev.payload = it.payload if it.payload is not None else prev.payload
            prev.status = it.status or prev.status
    if not merged:
        return {}
    rows = [
        {
            "key": it.key,
            "kind": it.kind,
            "label": it.label,
            "depth": it.depth,
            "by": sorted(set(it.discovered_by)),
            "status": it.status,
            "payload": it.payload,
        }
        for it in merged.values()
    ]
    out = conn.execute(UPSERT_SQL, {"source": source, "items": Jsonb(rows)}).fetchall()
    return {r["key"]: dict(r) for r in out}


def top(
    conn: psycopg.Connection,
    source: str,
    *,
    limit: int,
    status: str | None = "new",
    kind: str | None = None,
    max_depth: int | None = None,
    min_depth: int | None = None,
    exclude: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Highest score first (ties: most followers in the payload, then key), for "what next"."""
    return conn.execute(
        """select key, kind, label, score, depth, discovered_by, status, payload
             from frontier
            where source = %(source)s
              and (%(status)s::text is null or status = %(status)s)
              and (%(kind)s::text is null or kind = %(kind)s)
              and (%(depth)s::int is null or depth <= %(depth)s)
              and (%(min_depth)s::int is null or depth >= %(min_depth)s)
              and key <> all(%(exclude)s::text[])
            order by score desc,
                     coalesce((payload->'public_metrics'->>'followers_count')::bigint, 0) desc,
                     key
            limit %(limit)s""",
        {
            "source": source,
            "status": status,
            "kind": kind,
            "depth": max_depth,
            "min_depth": min_depth,
            "exclude": sorted(set(exclude)),
            "limit": limit,
        },
    ).fetchall()


def mark(conn: psycopg.Connection, source: str, keys: Iterable[str], status: str) -> None:
    conn.execute(
        """update frontier set status = %s,
                  fetched_at = case when %s = 'fetched' then now() else fetched_at end
            where source = %s and key = any(%s::text[])""",
        (status, status, source, sorted(set(keys))),
    )


# ── seeds config ──────────────────────────────────────────────────────────


@dataclass
class ExaQuery:
    q: str
    category: str | None = None  # Exa category: 'news', 'company', ...
    days: int | None = None  # only results published in the last N days
    num: int = 25
    include_domains: list[str] = field(default_factory=list)  # restrict to these sites (named startups)


@dataclass
class SeedsConfig:
    seeds: dict[str, list[str]] = field(default_factory=dict)  # seed set -> X usernames
    exa_queries: list[ExaQuery] = field(default_factory=list)
    path: Path | None = None


def seeds_path(env: Mapping[str, str] | None = None) -> Path:
    """``$SEEDS_FILE`` if set, else ``config/seeds.yaml`` in the nearest ancestor that has it."""
    env = os.environ if env is None else env
    if env.get("SEEDS_FILE"):
        return Path(env["SEEDS_FILE"]).expanduser()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "seeds.yaml").is_file():
            return parent / "config" / "seeds.yaml"
    return Path.cwd() / "config" / "seeds.yaml"


def load_seeds(env: Mapping[str, str] | None = None, path: Path | None = None) -> SeedsConfig:
    p = path or seeds_path(env)
    data = parse_yaml(p.read_text(encoding="utf-8")) if p.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected a mapping at the top level")
    seeds: dict[str, list[str]] = {}
    for name, handles in (data.get("seeds") or {}).items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", str(name)):
            raise ValueError(f"{p}: seed set name {name!r} must match [a-z0-9-]+ (it names a worker)")
        hs = handles or []
        if not isinstance(hs, list):
            raise ValueError(f"{p}: seed set {name!r} must be a list of X usernames")
        seeds[str(name)] = [str(h).lstrip("@").strip().lower() for h in hs if str(h).strip()]
    queries: list[ExaQuery] = []
    for q in ((data.get("exa") or {}).get("queries")) or []:
        if isinstance(q, str):
            queries.append(ExaQuery(q=q))
        elif isinstance(q, dict) and q.get("q"):
            queries.append(
                ExaQuery(
                    q=str(q["q"]),
                    category=str(q["category"]) if q.get("category") else None,
                    days=int(q["days"]) if q.get("days") not in (None, "") else None,
                    num=min(25, int(q.get("num") or 25)),
                    include_domains=[str(d) for d in (q.get("include_domains") or [])],
                )
            )
        else:
            raise ValueError(f"{p}: each exa query is a string or a mapping with 'q'")
    return SeedsConfig(seeds=seeds, exa_queries=queries, path=p)


# A small YAML subset (the worker has no YAML dependency): nested mappings by indentation,
# block lists ("- x", "- k: v" with following "  k2: v2"), flow lists ("[a, b]"), quoted or
# plain scalars, ints, true/false/null, and "#" comments. Enough for config/seeds.yaml.


def parse_yaml(text: str) -> Any:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if line.strip():
            if "\t" in line[: len(line) - len(line.lstrip())]:
                raise ValueError("tabs are not allowed for indentation")
            lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    value, i = _block(lines, 0, lines[0][0] if lines else 0)
    if i != len(lines):
        raise ValueError(f"unexpected indentation near {lines[i][1]!r}")
    return value


def _block(lines: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    if i >= len(lines):
        return None, i
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        out_l: list[Any] = []
        while (
            i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-")
        ):
            rest = lines[i][1][1:].strip()
            if not rest:
                v, i = _block(lines, i + 1, lines[i + 1][0] if i + 1 < len(lines) else indent + 2)
                out_l.append(v)
            elif _is_key(rest):
                # "- k: v" starts a mapping whose other keys are indented to the first key.
                sub = [(indent + 2, rest), *[]]
                j = i + 1
                while j < len(lines) and lines[j][0] > indent:
                    sub.append(lines[j])
                    j += 1
                v, _ = _block(sub, 0, indent + 2)
                out_l.append(v)
                i = j
            else:
                out_l.append(_scalar(rest))
                i += 1
        return out_l, i
    out: dict[str, Any] = {}
    while i < len(lines) and lines[i][0] == indent:
        text = lines[i][1]
        if not _is_key(text):
            raise ValueError(f"expected 'key: value', got {text!r}")
        key, _, rest = text.partition(":")
        key = _unquote(key.strip())
        rest = rest.strip()
        if rest:
            out[key] = _scalar(rest)
            i += 1
        elif i + 1 < len(lines) and lines[i + 1][0] > indent:
            out[key], i = _block(lines, i + 1, lines[i + 1][0])
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            out[key], i = _block(lines, i + 1, indent)  # list at the same indent as its key
        else:
            out[key] = None
            i += 1
    return out, i


_KEY_RE = re.compile(r"""^(?:"[^"]*"|'[^']*'|[^\s"'\[\]{}#:][^:#]*?)\s*:(?:\s|$)""")


def _is_key(s: str) -> bool:
    return bool(_KEY_RE.match(s))


def _strip_comment(line: str) -> str:
    quote: str | None = None
    for idx, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            return line[:idx]
    return line


def _scalar(s: str) -> Any:
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(p) for p in _split_flow(inner)] if inner else []
    if s in ("null", "~"):
        return None
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return _unquote(s)


def _split_flow(s: str) -> list[str]:
    parts, cur, quote = [], "", None
    for ch in s:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s
