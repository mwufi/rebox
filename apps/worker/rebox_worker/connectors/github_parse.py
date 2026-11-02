"""GitHub bio parser (``public/github_ai``, worker ``github-parser-*``).

Upstream: ``github`` profile records (``{"user": ..., "orgs": ...}``). The deterministic github
connector already turns ``company: "@org"`` into ``worked_at``; this parser reads the free text
(bio, plain-text company, location) for roles, past orgs, founder status and ``based_in``.
"""

from __future__ import annotations

from ..parsers.base import ParserConnector
from ..parsers.prompt import InputDoc


class GitHubParse(ParserConnector):
    name = "github_parse"
    layer = "public/github_ai"
    interval_s = 86400
    upstream = ("github",)
    worker_name = "github"
    batch_size = 10

    def accepts(self, doc: InputDoc) -> bool:
        return doc.kind == "github_bio"
