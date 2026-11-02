"""Wikidata (``public/wikidata``, CC0): organizations headquartered in San Francisco.

fetch
    1. **Discover** with one simple query: every item with headquarters location (P159) in a
       configured city (default San Francisco, Q62; ``WIKIDATA_CITIES=Q62,Q49255`` adds more),
       with its classes (P31), official websites (P856) and inception (P571). Transitive
       ``P31/P279*`` filters time out on the public endpoint, so classes are filtered here in
       Python (``EXCLUDED_CLASSES``: people, places, schools, museums, government bodies,
       periodicals, sports teams, ...). Newest organizations come first (inception desc), so a
       ``--limit`` dev run sees the startup end of the list.
    2. **Details** for ``BATCH`` organizations per query (label, description, P31, P856, P2002,
       P2037, P571, P1128 with rank + point in time, P112, P169 with rank + start/end).
    3. **People** named as founder (P112) or CEO (P169): label, description, P31, P856, P2002,
       P2037 in one query per batch.

    One RawRecord per organization, ``external_id = <QID>``, whose payload groups all of its
    bindings (and those of its people) in a canonical order, so an unchanged item hashes the
    same and is skipped. No cursor: a full refresh weekly. Requests are spaced
    ``MIN_INTERVAL_S`` apart, with backoff on 429/5xx.

extract (pure)
    company  identity ``wikidata`` (+ ``domain``, ``github``, ``x_handle`` so other sources'
             records of the same company land on it); ``name``, ``description``, ``website``,
             ``domain`` (from a website on the company's own host; not when another in-scope
             item has the same domain, e.g. Mozilla Foundation / Mozilla Corporation),
             ``x_handle``, ``github``, ``founded_year``, ``headcount``, ``based_in`` -> city.
    person   (founders and CEOs that are humans, P31 = Q5): identity ``wikidata`` (+ x/github),
             context = [company]; ``name``, ``description``, ``website``, ``x_handle``,
             ``github``; edges ``founded`` and ``ceo_of`` (valid_from/valid_to from P580/P582).
    place    the HQ city: identity ``wikidata``; ``name``.

source_ref: ``wikidata:<subject QID>:<property or predicate>[:<object key>]``, e.g.
``wikidata:Q7624104:P112:Q7146257``. Facts about a person or city use that item's QID, so the
same fact seen from two organizations is one claim.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import httpx

from ..models import ClaimInput, CursorUpdate, EntityRef, RawRecord
from .base import Ctx
from .hn import get_json, site_domain

ENDPOINT = "https://query.wikidata.org/sparql"
ENTITY = "http://www.wikidata.org/entity/"
DEFAULT_CITIES = ("Q62",)  # San Francisco
BATCH = 50
MIN_INTERVAL_S = 2.0
HUMAN = "Q5"

# Classes whose items are not companies for this graph. An item is kept when at least one of
# its classes is not in this set (a "business" that is also a "website" stays).
EXCLUDED_CLASSES = frozenset(
    {
        HUMAN,
        # places and structures
        "Q515",
        "Q1549591",
        "Q811979",
        "Q811430",
        "Q863454",
        "Q41176",
        "Q11315",
        "Q41253",
        "Q153562",
        "Q1076486",
        "Q167346",
        "Q43501",
        "Q2281788",
        "Q148319",
        "Q728937",
        "Q610237",
        # education and research
        "Q2467461",
        "Q180958",
        "Q3918",
        "Q3914",
        "Q189004",
        "Q9826",
        "Q875538",
        "Q902104",
        "Q446901",
        "Q5259432",
        "Q23002054",
        "Q23002052",
        "Q23002039",
        "Q2385804",
        "Q428602",
        "Q1021290",
        "Q383092",
        "Q414147",
        # museums, culture, performing arts
        "Q33506",
        "Q207694",
        "Q588140",
        "Q1970365",
        "Q16735822",
        "Q28737012",
        "Q54934129",
        "Q97662266",
        "Q1007870",
        "Q726929",
        "Q3152824",
        "Q20897549",
        "Q805233",
        "Q20819922",
        "Q239582",
        "Q207338",
        "Q215380",
        "Q2088357",
        "Q61653556",
        "Q742421",
        "Q11812394",
        "Q220505",
        "Q62018250",
        "Q8719053",
        "Q2980154",
        "Q1400264",
        # government, law enforcement, politics, religion, military
        "Q327333",
        "Q30559504",
        "Q732717",
        "Q1758690",
        "Q96080490",
        "Q691154",
        "Q7278",
        "Q60589804",
        "Q4481787",
        "Q7835189",
        "Q1110684",
        "Q176799",
        "Q105390172",
        "Q60176300",
        "Q1530022",
        "Q94670589",
        "Q23955632",
        "Q17127659",
        "Q276548",
        # sports
        "Q476028",
        "Q94579592",
        "Q13027888",
        "Q13393265",
        "Q14645593",
        "Q12973014",
        "Q847017",
        "Q124059801",
        "Q1855011",
        # publications (the publisher is the company, not the periodical)
        "Q11032",
        "Q41298",
        "Q1002697",
        "Q737498",
        "Q5633421",
        "Q1110794",
        "Q2305295",
        "Q2006125",
        "Q61851987",
        "Q1368848",
        "Q685935",
        "Q113772731",
        "Q773668",
        "Q73365221",
        "Q98561135",
        "Q6545508",
        "Q181298",
        "Q847906",
        # other non-organizations
        "Q13406463",
        "Q72610003",
        "Q33120876",
        "Q17558136",
        "Q13479982",
        "Q49759",
        "Q155511",
        "Q11707",
    }
)

DISCOVER_QUERY = """
SELECT ?item ?city ?cityLabel ?cls ?website ?inception WHERE {{
  VALUES ?city {{ {cities} }}
  ?item wdt:P159 ?city .
  ?city rdfs:label ?cityLabel . FILTER(LANG(?cityLabel) = "en")
  OPTIONAL {{ ?item wdt:P31 ?cls }}
  OPTIONAL {{ ?item wdt:P856 ?website }}
  OPTIONAL {{ ?item wdt:P571 ?inception }}
}}
"""

HANDLES_QUERY = """
SELECT ?item ?x ?gh WHERE {{
  VALUES ?city {{ {cities} }}
  ?item wdt:P159 ?city .
  {{ ?item wdt:P2002 ?x }} UNION {{ ?item wdt:P2037 ?gh }}
}}
"""

ORG_QUERY = """
SELECT ?item ?prop ?value ?rank ?time ?start ?end WHERE {{
  VALUES ?item {{ {items} }}
  {{ ?item rdfs:label ?value . FILTER(LANG(?value) IN ("en", "mul")) BIND("label" AS ?prop) }}
  UNION {{ ?item schema:description ?value . FILTER(LANG(?value) = "en") BIND("description" AS ?prop) }}
  UNION {{ ?item wdt:P31 ?value . BIND("P31" AS ?prop) }}
  UNION {{ ?item wdt:P856 ?value . BIND("P856" AS ?prop) }}
  UNION {{ ?item wdt:P2002 ?value . BIND("P2002" AS ?prop) }}
  UNION {{ ?item wdt:P2037 ?value . BIND("P2037" AS ?prop) }}
  UNION {{ ?item wdt:P571 ?value . BIND("P571" AS ?prop) }}
  UNION {{ ?item wdt:P112 ?value . BIND("P112" AS ?prop) }}
  UNION {{ ?item p:P1128 ?st . ?st ps:P1128 ?value ; wikibase:rank ?rank .
           OPTIONAL {{ ?st pq:P585 ?time }} BIND("P1128" AS ?prop) }}
  UNION {{ ?item p:P169 ?st . ?st ps:P169 ?value ; wikibase:rank ?rank .
           OPTIONAL {{ ?st pq:P580 ?start }} OPTIONAL {{ ?st pq:P582 ?end }} BIND("P169" AS ?prop) }}
}}
"""

PERSON_QUERY = """
SELECT ?item ?prop ?value WHERE {{
  VALUES ?item {{ {items} }}
  {{ ?item rdfs:label ?value . FILTER(LANG(?value) IN ("en", "mul")) BIND("label" AS ?prop) }}
  UNION {{ ?item schema:description ?value . FILTER(LANG(?value) = "en") BIND("description" AS ?prop) }}
  UNION {{ ?item wdt:P31 ?value . BIND("P31" AS ?prop) }}
  UNION {{ ?item wdt:P856 ?value . BIND("P856" AS ?prop) }}
  UNION {{ ?item wdt:P2002 ?value . BIND("P2002" AS ?prop) }}
  UNION {{ ?item wdt:P2037 ?value . BIND("P2037" AS ?prop) }}
}}
"""

Binding = dict[str, str]


class Wikidata:
    name = "wikidata"
    layer = "public/wikidata"
    interval_s = 604800  # weekly full refresh; unchanged records are skipped by content hash

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = MIN_INTERVAL_S,
    ) -> None:
        self.transport = transport
        self.sleep = sleep
        self.min_interval_s = min_interval_s

    # ── fetch: the only part that does I/O ──
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]:
        cities = _cities(ctx.env.get("WIKIDATA_CITIES"))
        with ctx.http(transport=self.transport) as client:

            def sparql(query: str) -> list[Binding]:
                data = get_json(
                    client,
                    ENDPOINT,
                    params={"query": query, "format": "json"},
                    headers={"Accept": "application/sparql-results+json"},
                    log=ctx.log,
                    sleep=self.sleep,
                    min_interval_s=self.min_interval_s,
                )
                return [_flat(b) for b in data["results"]["bindings"]]

            values = " ".join(f"wd:{q}" for q in cities)
            found = discover(sparql(DISCOVER_QUERY.format(cities=values)))
            add_handles(found, sparql(HANDLES_QUERY.format(cities=values)))
            orgs = [q for q, info in found.items() if keep_org(info["classes"])]
            orgs.sort(key=lambda q: (-(found[q]["inception_year"] or 0), int(q[1:])))
            if ctx.limit is not None:
                orgs = orgs[: ctx.limit]
            shared = shared_domains(found, orgs)
            ctx.log.info("wikidata: %d items in %s, %d kept as organizations", len(found), cities, len(orgs))

            yield from self.records(sparql, found, orgs, shared)

    def records(
        self,
        sparql: Callable[[str], list[Binding]],
        found: dict[str, dict[str, Any]],
        orgs: list[str],
        shared: dict[str, set[str]],
    ) -> Iterator[RawRecord | CursorUpdate]:
        """Detail queries for ``orgs`` (from ``discover``), BATCH at a time -> RawRecords."""
        done = 0
        for batch in _batches(orgs, BATCH):
            rows = sparql(ORG_QUERY.format(items=" ".join(f"wd:{q}" for q in batch)))
            by_org = _group(rows)
            people = sorted(
                {q for r in rows if r.get("prop") in ("P112", "P169") and (q := _qid(r.get("value")))},
                key=lambda q: int(q[1:]),
            )
            by_person: dict[str, list[Binding]] = {}
            for pbatch in _batches(people, BATCH):
                by_person.update(
                    _group(sparql(PERSON_QUERY.format(items=" ".join(f"wd:{q}" for q in pbatch))))
                )
            for qid in batch:
                org_rows = by_org.get(qid, [])
                linked = sorted(
                    {
                        q
                        for r in org_rows
                        if r.get("prop") in ("P112", "P169") and (q := _qid(r.get("value")))
                    },
                    key=lambda q: int(q[1:]),
                )
                payload = {
                    "qid": qid,
                    "hq": [
                        {"qid": c, "label": found[qid]["cities"][c]} for c in sorted(found[qid]["cities"])
                    ],
                    "bindings": _canonical(org_rows),
                    "people": {p: _canonical(by_person.get(p, [])) for p in linked},
                    "domain_shared_with": sorted(shared.get(qid, ())),
                    "handles_shared": shared_handles(found, qid),
                }
                yield RawRecord.new(self.name, qid, payload, url=f"https://www.wikidata.org/wiki/{qid}")
            done += len(batch)
            yield CursorUpdate(cursor={"refreshed": done, "of": len(orgs)})

    # ── extract: pure ──
    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        p: dict[str, Any] = rec.payload
        qid = p["qid"]
        org = _props(p.get("bindings", []))
        name = _label(org)
        if not name:
            return []  # an item without an English label is not usable yet
        out: list[ClaimInput] = []

        def add(subject: EntityRef, predicate: str, ref: str, **kw: Any) -> None:
            out.append(
                ClaimInput(subject=subject, predicate=predicate, source_ref=f"{self.name}:{ref}", **kw)
            )

        websites = sorted(set(org.get("P856", [])))
        shared = bool(p.get("domain_shared_with"))
        domains = [] if shared else sorted({d for w in websites if (d := company_domain(w, name))})
        x_handles = sorted(set(org.get("P2002", [])), key=str.lower)
        githubs = sorted(set(org.get("P2037", [])), key=str.lower)
        # Handles another in-scope item also carries (GitHub and GitHub, Inc. both list @github)
        # stay facts but are not identity, or the second item would be matched onto the first.
        not_id = set(p.get("handles_shared") or [])
        identity: dict[str, Any] = {
            "wikidata": qid,
            "domain": domains,
            "github": [g for g in githubs if f"github:{g.lower()}" not in not_id],
            "x_handle": [h for h in x_handles if f"x_handle:{h.lower()}" not in not_id],
        }
        company = EntityRef(kind="company", identity=identity, name=name)

        add(company, "name", f"{qid}:name", value=name)
        add(company, "wikidata", f"{qid}:wikidata", value=qid)
        if desc := _first(org.get("description")):
            add(company, "description", f"{qid}:description", value=desc)
        for w in websites:
            add(company, "website", f"{qid}:P856:{w}", value=w)
        for d in domains:
            add(company, "domain", f"{qid}:domain:{d}", value=d)
        for h in x_handles:
            add(company, "x_handle", f"{qid}:P2002:{h.lower()}", value=h)
        for g in githubs:
            add(company, "github", f"{qid}:P2037:{g.lower()}", value=g)
        if (year := founded_year(org.get("P571", []))) is not None:
            add(company, "founded_year", f"{qid}:P571", value=year)
        if (hc := headcount(p.get("bindings", []))) is not None:
            add(company, "headcount", f"{qid}:P1128", value=hc)
        for city in p.get("hq", []):
            place = EntityRef(kind="place", identity={"wikidata": city["qid"]}, name=city["label"])
            add(place, "name", f"{city['qid']}:name", value=city["label"])
            add(place, "wikidata", f"{city['qid']}:wikidata", value=city["qid"])
            add(company, "based_in", f"{qid}:P159:{city['qid']}", object=place)

        # People: founders (P112) and CEOs (P169) that are humans.
        company_ctx = EntityRef(kind="company", identity=identity, name=name)
        people: dict[str, EntityRef] = {}
        for pq, rows in sorted(p.get("people", {}).items()):
            props = _props(rows)
            pname = _label(props)
            if HUMAN not in {_qid(c) for c in props.get("P31", [])} or not pname:
                continue  # founded by an organization, or no English label
            pid = {
                "wikidata": pq,
                "x_handle": sorted(set(props.get("P2002", [])), key=str.lower),
                "github": sorted(set(props.get("P2037", [])), key=str.lower),
            }
            person = EntityRef(kind="person", identity=pid, name=pname, context=[company_ctx])
            people[pq] = person
            add(person, "name", f"{pq}:name", value=pname)
            add(person, "wikidata", f"{pq}:wikidata", value=pq)
            if desc := _first(props.get("description")):
                add(person, "description", f"{pq}:description", value=desc)
            for w in sorted(set(props.get("P856", []))):
                add(person, "website", f"{pq}:P856:{w}", value=w)
            for h in pid["x_handle"]:
                add(person, "x_handle", f"{pq}:P2002:{h.lower()}", value=h)
            for g in pid["github"]:
                add(person, "github", f"{pq}:P2037:{g.lower()}", value=g)

        for pq in sorted({_qid(v) for v in org.get("P112", [])} & people.keys()):
            add(people[pq], "founded", f"{qid}:P112:{pq}", object=company)
        for st in ceo_statements(p.get("bindings", [])):
            if st["person"] not in people:
                continue
            key = f"{qid}:P169:{st['person']}" + (f":{st['start'][:10]}" if st["start"] else "")
            add(
                people[st["person"]],
                "ceo_of",
                key,
                object=company,
                valid_from=st["start"],
                valid_to=st["end"],
            )
        return out


# ── pure helpers ──────────────────────────────────────────────────────────


def _cities(env_value: str | None) -> tuple[str, ...]:
    if not env_value:
        return DEFAULT_CITIES
    out = tuple(q.strip().upper() for q in env_value.split(",") if q.strip())
    if not out or not all(q[0] == "Q" and q[1:].isdigit() for q in out):
        raise ValueError(f"WIKIDATA_CITIES must be a comma-separated list of QIDs, got {env_value!r}")
    return out


def _flat(b: dict[str, Any]) -> Binding:
    out = {k: v["value"] for k, v in b.items()}
    if "xml:lang" in b.get("value", {}):
        out["lang"] = b["value"]["xml:lang"]  # labels: "en" or "mul"
    return out


def _qid(uri: str | None) -> str | None:
    if uri and uri.startswith(ENTITY):
        q = uri[len(ENTITY) :]
        if q[:1] == "Q" and q[1:].isdigit():
            return q
    return None


def _batches(items: list[str], n: int) -> Iterator[list[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def _group(rows: Iterable[Binding]) -> dict[str, list[Binding]]:
    out: dict[str, list[Binding]] = defaultdict(list)
    for r in rows:
        q = _qid(r.get("item"))
        if q:
            out[q].append({k: v for k, v in r.items() if k != "item"})
    return out


def _canonical(rows: Iterable[Binding]) -> list[Binding]:
    """De-duplicated, sorted bindings (SPARQL result order is not stable)."""
    uniq = {tuple(sorted(r.items())) for r in rows}
    return [dict(t) for t in sorted(uniq)]


def discover(rows: Iterable[Binding]) -> dict[str, dict[str, Any]]:
    """Discovery rows -> {qid: {classes, websites, inception_year, cities}}."""
    found: dict[str, dict[str, Any]] = {}
    for r in rows:
        q = _qid(r.get("item"))
        if not q:
            continue
        info = found.setdefault(
            q, {"classes": set(), "websites": set(), "inception_year": None, "cities": {}}
        )
        if c := _qid(r.get("cls")):
            info["classes"].add(c)
        if r.get("website"):
            info["websites"].add(r["website"])
        if (y := _year(r.get("inception"))) is not None:
            info["inception_year"] = y if info["inception_year"] is None else min(info["inception_year"], y)
        if (city := _qid(r.get("city"))) and r.get("cityLabel"):
            info["cities"][city] = r["cityLabel"]
    return found


def add_handles(found: dict[str, dict[str, Any]], rows: Iterable[Binding]) -> None:
    """Merge HANDLES_QUERY rows into ``discover`` output (lowercased: handles are case-blind)."""
    for r in rows:
        q = _qid(r.get("item"))
        if q not in found:
            continue
        hs = found[q].setdefault("handles", set())
        if r.get("x"):
            hs.add(f"x_handle:{r['x'].strip().lstrip('@').lower()}")
        if r.get("gh"):
            hs.add(f"github:{r['gh'].strip().lstrip('@').lower()}")


def shared_handles(found: dict[str, dict[str, Any]], qid: str) -> list[str]:
    """``qid``'s handles (``x_handle:<h>`` / ``github:<h>``) that another kept item also has."""
    mine = found[qid].get("handles", set())
    others: set[str] = set()
    for q, info in found.items():
        if q != qid and keep_org(info["classes"]):
            others |= info.get("handles", set()) & mine
    return sorted(others)


def keep_org(classes: set[str]) -> bool:
    """An organization worth a company entity: has a class, and not only excluded ones."""
    return bool(classes) and bool(classes - EXCLUDED_CLASSES)


def shared_domains(found: dict[str, dict[str, Any]], orgs: Iterable[str]) -> dict[str, set[str]]:
    """qid -> other in-scope items with the same website domain (their domains are not identity)."""
    by_domain: dict[str, set[str]] = defaultdict(set)
    for q, info in found.items():
        if not keep_org(info["classes"]):
            continue
        for w in info["websites"]:
            if d := site_domain(w, allow_platform_root=True):
                by_domain[d].add(q)
    out: dict[str, set[str]] = {}
    for q in orgs:
        for w in found[q]["websites"]:
            d = site_domain(w, allow_platform_root=True)
            if d and len(by_domain[d]) > 1:
                out.setdefault(q, set()).update(by_domain[d] - {q})
    return out


def company_domain(website: str, name: str) -> str | None:
    """The website's domain if it is the company's own site: the site root (``https://stripe.com/``,
    also a platform's own root such as ``https://github.com``), or a page on a host that matches
    the company name. ``https://books.google.com/x`` for a Google imprint is not its domain."""
    from urllib.parse import urlsplit

    from .hn import name_matches_host

    d = site_domain(website, allow_platform_root=True)
    if d is None:
        return None
    try:
        path = urlsplit(website if "://" in website else f"https://{website}").path.strip("/").lower()
    except ValueError:
        return None
    if path in ("", "en", "en-us", "us", "home", "index.html", "index.php"):
        return d
    return d if name_matches_host(name, d) else None


def _props(rows: Iterable[Binding]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        prop = r.get("prop")
        if prop == "label":
            prop = f"label@{r.get('lang', 'en')}"
        if prop and "value" in r:
            out[prop].append(r["value"])
    return out


def _label(props: dict[str, list[str]]) -> str | None:
    """The English label, else the language-neutral (``mul``) one."""
    for lang in ("en", "mul"):
        labels = sorted(set(props.get(f"label@{lang}", [])))
        if labels:
            return labels[0].strip() or None
    return None


def _first(values: list[str] | None) -> str | None:
    return sorted(values)[0] if values else None


def _year(value: str | None) -> int | None:
    """``2010-01-01T00:00:00Z`` -> 2010 (negative/BCE and malformed dates -> None)."""
    if not value or value[0] == "-":
        return None
    try:
        return int(value.lstrip("+")[:4])
    except ValueError:
        return None


def founded_year(values: list[str]) -> int | None:
    years = [y for v in values if (y := _year(v)) is not None and 1600 <= y <= 2100]
    return min(years) if years else None


_RANK = {
    "http://wikiba.se/ontology#PreferredRank": 2,
    "http://wikiba.se/ontology#NormalRank": 1,
    "http://wikiba.se/ontology#DeprecatedRank": 0,
}


def headcount(rows: Iterable[Binding]) -> int | None:
    """P1128: the best-ranked statement, latest point in time first (deprecated ignored)."""
    best: tuple[int, str, int] | None = None
    for r in rows:
        if r.get("prop") != "P1128":
            continue
        rank = _RANK.get(r.get("rank", ""), 1)
        if rank == 0:
            continue
        try:
            n = int(float(r["value"]))
        except (KeyError, ValueError):
            continue
        if n < 0:
            continue
        cand = (rank, r.get("time", ""), n)
        if best is None or cand > best:
            best = cand
    return best[2] if best else None


def ceo_statements(rows: Iterable[Binding]) -> list[dict[str, str | None]]:
    """P169 statements (deprecated ignored): person, start, end (ISO dates or None)."""
    out = []
    for r in rows:
        if r.get("prop") != "P169" or _RANK.get(r.get("rank", ""), 1) == 0:
            continue
        person = _qid(r.get("value"))
        if person:
            out.append({"person": person, "start": _date(r.get("start")), "end": _date(r.get("end"))})
    uniq = {tuple(sorted(s.items(), key=lambda kv: kv[0])) for s in out}
    return sorted((dict(t) for t in uniq), key=lambda s: (s["person"], s["start"] or ""))


def _date(value: str | None) -> str | None:
    """``2015-08-10T00:00:00Z`` -> ``2015-08-10`` (None for missing/BCE/unknown-value blanks)."""
    if not value or value[0] == "-" or not value[:4].isdigit():
        return None
    return value[:10]
