"""Connector registry: source name -> connector class. Add one line per connector."""

from __future__ import annotations

from .base import Connector, Ctx, fixtures_root, is_enabled, is_scheduled
from .exa import Exa
from .example import Example
from .github import GitHub
from .hn import Hn
from .wikidata import Wikidata
from .x import X
from .x_spider import XSpider


class UnknownConnector(KeyError):
    def __str__(self) -> str:
        return str(self.args[0])


REGISTRY: dict[str, type] = {
    Example.name: Example,
    Wikidata.name: Wikidata,
    Hn.name: Hn,
    GitHub.name: GitHub,
    X.name: X,
    XSpider.name: XSpider,
    Exa.name: Exa,
}


def get_connector(name: str) -> Connector:
    try:
        return REGISTRY[name]()
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "none"
        raise UnknownConnector(f"unknown connector {name!r} (known: {known})") from None


__all__ = [
    "REGISTRY",
    "Connector",
    "Ctx",
    "UnknownConnector",
    "fixtures_root",
    "get_connector",
    "is_enabled",
    "is_scheduled",
]
