"""Is an article *about* a company, or does it only mention it in passing? (S18)

Exa search results for a company include listicles ("SF dating culture this week"), roundups
and foreign aggregators that name the company once. Their milestones would land on the
company's timeline as if they were news about it. ``extract`` keeps an article milestone only
when:

- the company's name is in the article **title**, or in the **lead** (the first ``LEAD_CHARS``
  characters of the text); and
- the milestone's **quote** names the company, or the title does.

Pure string checks on what the model saw, so a replay applies them to stored model output with
no model calls.

Name matching is on word boundaries ("Known's" names Known, "Unknown" does not; only Latin
letters and digits count as word characters, so "Dittoが" names Ditto). A lowercase
occurrence of a capitalized name is not the name ("a different approach" does not name
"Different", "well known" does not name "Known"); an all-caps one is ("KNOWN RAISES $9.7M").
Trailing legal forms and parentheticals are optional ("Rocker, Inc." matches "Rocker").
"""

from __future__ import annotations

import re

LEAD_CHARS = 400

_LEGAL_RE = re.compile(r"(?i)[\s,]+(inc|llc|ltd|corp|corporation|co|gmbh|sa|plc)\.?$")
_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
# Word characters for name boundaries: Latin letters and digits only, so a name followed by a
# CJK particle ("Dittoが", "디토의") still matches, while "Unknown" does not name "Known".
_WORD = r"0-9A-Za-z_\u00C0-\u024F"


def name_variants(name: str) -> list[str]:
    """The name as written, without a trailing ``(...)``, without trailing legal forms."""
    base = " ".join(name.split()).lstrip("@")
    out = [base]
    s = _PAREN_RE.sub("", base).strip()
    while True:
        shorter = _LEGAL_RE.sub("", s).rstrip(" ,.")
        if not shorter or shorter == s:
            break
        s = shorter
    if s and s != base:
        out.append(s)
    return [v for v in out if v]


def names(name: str, text: str) -> bool:
    """``text`` names the company ``name`` (word boundaries; see the module docstring for case)."""
    if not name or not text:
        return False
    for v in name_variants(name):
        for m in re.finditer(rf"(?<![{_WORD}@]){re.escape(v)}(?![{_WORD}])", text, re.I):
            occ = m.group(0)
            if occ == v or not occ.islower():
                return True
    return False


def about(name: str, *, title: str, text: str, quote: str) -> bool:
    """The article is about ``name`` and the milestone's quote (or the title) names it."""
    in_title = names(name, title)
    if not (in_title or names(name, text[:LEAD_CHARS])):
        return False
    return in_title or names(name, quote)
