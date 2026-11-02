"""City names the parser may emit as ``based_in`` places, with their Wikidata ids.

Only cities in this table (or already in the graph under the same unique name, see
``links``) become ``based_in`` edges: a place known only by a name would be minted once per
record and duplicate. QIDs were checked against Wikidata (2026-11-02).
"""

from __future__ import annotations

from ..normalize import normalize_name

CITIES: dict[str, str] = {
    "San Francisco": "Q62",
    "New York City": "Q60",
    "Los Angeles": "Q65",
    "Oakland": "Q17042",
    "Berkeley": "Q484678",
    "Palo Alto": "Q47265",
    "Mountain View": "Q486860",
    "Menlo Park": "Q74195",
    "Sunnyvale": "Q208459",
    "Redwood City": "Q505549",
    "San Mateo": "Q169943",
    "San Jose": "Q16553",
    "Santa Monica": "Q47164",
    "San Diego": "Q16552",
    "Seattle": "Q5083",
    "Portland": "Q6106",
    "Austin": "Q16559",
    "Boston": "Q100",
    "Chicago": "Q1297",
    "Denver": "Q16554",
    "Boulder": "Q192517",
    "Miami": "Q8652",
    "Atlanta": "Q23556",
    "Dallas": "Q16557",
    "Houston": "Q16555",
    "Phoenix": "Q16556",
    "Philadelphia": "Q1345",
    "Pittsburgh": "Q1342",
    "Salt Lake City": "Q23337",
    "Washington, D.C.": "Q61",
    "Brooklyn": "Q18419",
    "Toronto": "Q172",
    "Vancouver": "Q24639",
    "Montreal": "Q340",
    "Mexico City": "Q1489",
    "São Paulo": "Q175",
    "London": "Q84",
    "Paris": "Q90",
    "Berlin": "Q64",
    "Munich": "Q1726",
    "Amsterdam": "Q727",
    "Dublin": "Q1761",
    "Stockholm": "Q1754",
    "Zurich": "Q72",
    "Madrid": "Q2807",
    "Barcelona": "Q1492",
    "Lisbon": "Q597",
    "Warsaw": "Q270",
    "Tel Aviv": "Q33935",
    "Dubai": "Q612",
    "Bengaluru": "Q1355",
    "Singapore": "Q334",
    "Hong Kong": "Q8646",
    "Tokyo": "Q1490",
    "Seoul": "Q8684",
    "Beijing": "Q956",
    "Shanghai": "Q8686",
    "Sydney": "Q3130",
    "Lagos": "Q8673",
    "Nairobi": "Q3870",
}

ALIASES: dict[str, str] = {
    "sf": "San Francisco",
    "s.f.": "San Francisco",
    "san fran": "San Francisco",
    "san francisco ca": "San Francisco",
    "san francisco california": "San Francisco",
    "bay area": "San Francisco",
    "sf bay area": "San Francisco",
    "san francisco bay area": "San Francisco",
    "the bay": "San Francisco",
    "nyc": "New York City",
    "new york": "New York City",
    "new york ny": "New York City",
    "ny": "New York City",
    "manhattan": "New York City",
    "la": "Los Angeles",
    "dc": "Washington, D.C.",
    "washington dc": "Washington, D.C.",
    "bangalore": "Bengaluru",
    "sao paulo": "São Paulo",
    "zürich": "Zurich",
}

_BY_KEY = {normalize_name(k): k for k in CITIES} | {normalize_name(k): v for k, v in ALIASES.items()}


def canonical_city(name: str) -> tuple[str, str] | None:
    """``"SF"`` -> ``("San Francisco", "Q62")``; unknown -> None."""
    key = normalize_name(name)
    city = _BY_KEY.get(key)
    if city is None and "," in name:  # "Austin, TX"
        city = _BY_KEY.get(normalize_name(name.split(",")[0]))
    return (city, CITIES[city]) if city else None
