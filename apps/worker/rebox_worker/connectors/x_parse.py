"""X parsers (``public/x_ai``): bios of ``x_user`` records and text of ``x_post`` records.

Upstream: the ``x_spider`` source (``x_user``, ``x_post``) and the ``x`` enrichment source
(profiles). Two connectors, so bios and posts are distinct workers (CONTRACTS §12):

- ``x_parse``        bios -> roles, founder/investor status, past orgs, ``based_in``
                     (worker ``xbio-parser-*``)
- ``x_parse_posts``  posts -> milestones, ``acquired``, hires (worker ``xpost-parser-*``)

Both are ``ParserConnector``s (see ``parsers/base.py`` and ``docs/PARSING.md``).
"""

from __future__ import annotations

from ..parsers.base import ParserConnector
from ..parsers.prompt import InputDoc


class XParse(ParserConnector):
    name = "x_parse"
    layer = "public/x_ai"
    interval_s = 3600
    upstream = ("x_spider", "x")
    worker_name = "xbio"
    batch_size = 10

    def accepts(self, doc: InputDoc) -> bool:
        return doc.kind == "x_bio"


class XParsePosts(ParserConnector):
    name = "x_parse_posts"
    layer = "public/x_ai"
    interval_s = 3600
    upstream = ("x_spider",)
    worker_name = "xpost"
    batch_size = 8

    def accepts(self, doc: InputDoc) -> bool:
        return doc.kind == "x_post"
