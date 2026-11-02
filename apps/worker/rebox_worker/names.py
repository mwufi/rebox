"""Name folding: the key two mentions of an entity's name must share to be "the same name".

``fold_name(kind, name)`` is pure and stable (it feeds deterministic ids, ``ids.mint_id`` with
``{"name": folded}``), so changing it changes the ids of name-minted companies: treat the rules
below as part of CONTRACTS §3.

Every kind: NFKC, casefold, strip accents, trim, collapse whitespace.

Companies also drop, in this order:
  1. one trailing ``(...)`` parenthetical            ``"Ramp (YC W19)"``      -> ``"ramp"``
  2. trailing punctuation (not closing brackets)     ``"Yahoo!"``             -> ``"yahoo"``
  3. a leading ``the ``                              ``"The Browser Company"`` -> ``"browser company"``
  4. legal suffixes after a space or comma, repeatedly, each followed by step 2 again
                                                     ``"Acme Co., Ltd."``     -> ``"acme"``
A step that would leave nothing is skipped (``"The"`` stays ``"the"``, ``"Inc."`` stays ``"inc"``).

Unlike ``normalize.normalize_name`` (the scoped fuzzy pass), inner punctuation is kept:
``"Y-Combinator"`` and ``"Y Combinator"`` fold differently.

Guards for matching by name (pure, on pre-fetched facts; the resolver's company name pass uses
them, and the dedupe pass can): ``is_safe_name_target`` (may a name-only mention link to this
keyed company?), ``domain_label`` and ``converges_by_name`` (is a name specific enough to
converge on at all?). ``PLACEHOLDER_COMPANY_NAMES`` never converge.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

LEGAL_SUFFIXES: tuple[str, ...] = (
    "inc",
    "inc.",
    "llc",
    "l.l.c.",
    "ltd",
    "ltd.",
    "corp",
    "corp.",
    "corporation",
    "co",
    "co.",
    "gmbh",
    "s.a.",
    "sa",
    "plc",
    "pbc",
)

# Trailing punctuation is stripped before suffixes are matched, so "inc." arrives as "inc" and
# "l.l.c." as "l.l.c": match each suffix with its final dot optional.
_SUFFIX_RE = re.compile(
    r"(?<=\S)[\s,]+(?:"
    + "|".join(sorted({re.escape(s.rstrip(".")) + r"\.?" for s in LEGAL_SUFFIXES}, key=len, reverse=True))
    + r")$"
)
_PAREN_RE = re.compile(r"(?<=\S)\s*\([^()]*\)$")

# Names that say "no company" or "an undisclosed company". They never converge by name (the
# resolver keeps minting them per record) — "Founder @ Stealth" in two bios is two companies.
PLACEHOLDER_COMPANY_NAMES: frozenset[str] = frozenset(
    {
        "stealth",
        "stealth mode",
        "stealth startup",
        "stealth mode startup",
        "stealth company",
        "a stealth startup",
        "startup",
        "a startup",
        "my startup",
        "self-employed",
        "self employed",
        "freelance",
        "freelancer",
        "independent",
        "unemployed",
        "retired",
        "none",
        "n/a",
    }
)


def _strip_accents(s: str) -> str:
    decomposed = unicodedata.normalize("NFKD", s)
    return unicodedata.normalize("NFC", "".join(ch for ch in decomposed if not unicodedata.combining(ch)))


def _is_trailing_junk(ch: str) -> bool:
    # Closing brackets (Pe) stay: "acme (formerly x) inc" folds to "acme (formerly x)", not "...x".
    cat = unicodedata.category(ch)
    return ch.isspace() or (cat.startswith("P") and cat != "Pe")


def _strip_trailing_punct(s: str) -> str:
    end = len(s)
    while end > 0 and _is_trailing_junk(s[end - 1]):
        end -= 1
    return s[:end] if end else s


def fold_base(name: str) -> str:
    """NFKC, casefold, strip accents, trim, collapse whitespace (every kind)."""
    s = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(_strip_accents(s).split())


def fold_name(kind: str, name: str | None) -> str:
    """The folded name of an entity of ``kind`` (see the module docstring). ``""`` for no name."""
    if not name:
        return ""
    s = fold_base(name)
    if kind != "company" or not s:
        return s
    s = _PAREN_RE.sub("", s).rstrip()
    s = _strip_trailing_punct(s)
    if s.startswith("the ") and s[4:].strip():
        s = s[4:].lstrip()
    while (m := _SUFFIX_RE.search(s)) is not None:
        s = _strip_trailing_punct(s[: m.start()])
    return s


def is_placeholder_company(folded: str) -> bool:
    """A folded company name that names no particular company (e.g. ``"stealth"``)."""
    return folded in PLACEHOLDER_COMPANY_NAMES


# ── name-match guards (resolver pass 3; reusable by the dedupe pass) ──────

# Two-label public suffixes common enough to matter; the registrable label sits left of them.
# Not the full Public Suffix List: a domain under an unlisted 2-part suffix yields the wrong
# label, which can only make a target look *less* safe (the label will not equal a name).
TWO_PART_SUFFIXES: frozenset[str] = frozenset(
    {
        *(f"{sld}.uk" for sld in ("co", "org", "ac", "gov", "ltd", "plc", "me", "net")),
        *(f"{sld}.au" for sld in ("com", "net", "org", "edu", "gov", "id")),
        *(f"{sld}.nz" for sld in ("co", "org", "net", "ac", "govt")),
        *(f"{sld}.jp" for sld in ("co", "ne", "or", "ac", "go")),
        *(f"{sld}.kr" for sld in ("co", "or", "ac", "go")),
        *(f"{sld}.in" for sld in ("co", "net", "org", "firm", "gen", "ind")),
        *(f"{sld}.za" for sld in ("co", "org", "net")),
        *(f"{sld}.il" for sld in ("co", "org", "ac")),
        *(f"{sld}.br" for sld in ("com", "net", "org")),
        *(f"{sld}.cn" for sld in ("com", "net", "org")),
        *(f"{sld}.hk" for sld in ("com", "org", "net")),
        *(f"{sld}.sg" for sld in ("com", "org", "net", "edu")),
        *(f"{sld}.tw" for sld in ("com", "org", "net")),
        *(f"{sld}.mx" for sld in ("com", "org")),
        *(f"{sld}.ar" for sld in ("com", "org")),
        *(f"{sld}.tr" for sld in ("com", "org", "net")),
        *(f"{sld}.my" for sld in ("com", "org", "net")),
        *(f"{sld}.ph" for sld in ("com", "org", "net")),
        *(f"{sld}.id" for sld in ("co", "or", "ac")),
        *(f"{sld}.th" for sld in ("co", "or", "ac")),
    }
)

MIN_INCOMING_SUBJECTS = 5  # (c): distinct subjects with a non-`follows` edge to the target
NAME_MINT_MIN_CHARS = 6  # a single-token folded name shorter than this never converges by name


def squash(s: str) -> str:
    """Only the letters and digits of ``s`` (``"coca-cola"`` -> ``"cocacola"``)."""
    return "".join(ch for ch in s if ch.isalnum())


def domain_label(domain: str) -> str | None:
    """The registrable label of a (normalized, CONTRACTS §4) domain: the label left of the
    public suffix. ``google.com`` -> ``google``, ``labs.ramp.com`` -> ``ramp``,
    ``bbc.co.uk`` -> ``bbc``. ``None`` when there is no such label (``co.uk``, ``localhost``)."""
    labels = [label for label in domain.strip().lower().rstrip(".").split(".") if label]
    if len(labels) < 2:
        return None
    suffix_len = 2 if ".".join(labels[-2:]) in TWO_PART_SUFFIXES else 1
    if len(labels) <= suffix_len:
        return None
    return labels[-suffix_len - 1]


def is_safe_name_target(
    folded: str,
    *,
    has_wikidata: bool,
    domains: Iterable[str] = (),
    incoming_subjects: int = 0,
) -> bool:
    """May a company ref known only by the (folded) name ``folded`` be linked to this keyed
    company? Exact names alone are not enough for generic words ("Capital", "Base",
    "Balance"): the target must also (a) have a ``wikidata`` claim, or (b) own a domain whose
    registrable label is the name (``notion.so`` for "notion"), or (c) be the object of a
    non-``follows`` edge from at least ``MIN_INCOMING_SUBJECTS`` distinct subjects."""
    if has_wikidata or incoming_subjects >= MIN_INCOMING_SUBJECTS:
        return True
    key = squash(folded)
    return bool(key) and any(squash(domain_label(d) or "") == key for d in domains)


def converges_by_name(folded: str) -> bool:
    """May mentions of this folded company name converge without a safe keyed target (match
    an existing name-only company, or mint ``{"name": folded}``)? Only names specific enough:
    at least two tokens, or at least ``NAME_MINT_MIN_CHARS`` characters."""
    return len(folded.split()) >= 2 or len(folded) >= NAME_MINT_MIN_CHARS
