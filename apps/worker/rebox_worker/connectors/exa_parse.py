"""Exa article parser (``public/exa_ai``, worker ``exa-parser-*``).

Upstream: ``exa`` results (``url``, ``title``, ``publishedDate``, ``text``). Company homepages
(a URL with no path) are left to the deterministic ``exa`` connector; articles become
milestones (launches, fundraises, hires) and ``acquired`` edges, dated by ``publishedDate``.

Relevance (S18, ``parsers/relevance.py``): a milestone is kept only when the article is *about*
its company (named in the title or the first 400 characters) and the quote or title names it.
Dropped milestones are counted in the run stats as ``irrelevant_dropped``; the same milestone
twice from one article as ``duplicate_milestones``. Both are post-filters on the stored model
output, so ``rebox-worker replay exa_parse`` applies them with no model calls.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..models import ClaimInput, RawRecord
from ..parsers.base import ParserConnector
from ..parsers.extract import DUPLICATE_MILESTONE, IRRELEVANT, extract_with_drops
from ..parsers.prompt import InputDoc


class ExaParse(ParserConnector):
    name = "exa_parse"
    layer = "public/exa_ai"
    interval_s = 3600
    upstream = ("exa",)
    worker_name = "exa"
    batch_size = 4

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.irrelevant_dropped = 0
        self.duplicate_milestones = 0

    def run_stats(self) -> dict[str, Any]:
        """Lands in the run's stats (pipeline ``connector_run_stats``)."""
        return {
            "irrelevant_dropped": self.irrelevant_dropped,
            "duplicate_milestones": self.duplicate_milestones,
        }

    def accepts(self, doc: InputDoc) -> bool:
        if doc.kind != "article" or not doc.meta.get("date"):
            return False
        path = urlsplit(str(doc.meta.get("url") or "")).path
        return path not in ("", "/")

    def extract(self, rec: RawRecord) -> list[ClaimInput]:
        if not isinstance(rec.payload, dict) or "input" not in rec.payload:
            return []
        claims, dropped = extract_with_drops(self.name, rec.payload)
        self.irrelevant_dropped += sum(1 for reason, _ in dropped if reason == IRRELEVANT)
        self.duplicate_milestones += sum(1 for reason, _ in dropped if reason == DUPLICATE_MILESTONE)
        return claims
