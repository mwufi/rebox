"""Exa article parser (``public/exa_ai``, worker ``exa-parser-*``).

Upstream: ``exa`` results (``url``, ``title``, ``publishedDate``, ``text``). Company homepages
(a URL with no path) are left to the deterministic ``exa`` connector; articles become
milestones (launches, fundraises, hires) and ``acquired`` edges, dated by ``publishedDate``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from ..parsers.base import ParserConnector
from ..parsers.prompt import InputDoc


class ExaParse(ParserConnector):
    name = "exa_parse"
    layer = "public/exa_ai"
    interval_s = 3600
    upstream = ("exa",)
    worker_name = "exa"
    batch_size = 4

    def accepts(self, doc: InputDoc) -> bool:
        if doc.kind != "article" or not doc.meta.get("date"):
            return False
        path = urlsplit(str(doc.meta.get("url") or "")).path
        return path not in ("", "/")
