"""Parser tests run offline: network is blocked and the model is a recording (``RecordedModel``).

Model outputs are recorded live (``eval_parser.py --record``, ``record_fixtures.py``) and
committed under ``fixtures/raw/{x_parse,exa_parse}/``; every input in them is synthetic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rebox_worker.parsers.llm import Usage  # noqa: E402
from rebox_worker.parsers.prompt import InputDoc  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures" / "raw"

# Network guard shared with the connector tests.
from connectors.conftest import no_network  # noqa: E402, F401


class RecordedModel:
    """A ``Model`` that answers from a dict ``input text -> output`` and counts its calls."""

    name = "claude-haiku-5-5"

    def __init__(self, outputs: dict[str, dict[str, Any] | None], *, strict: bool = True) -> None:
        self.outputs = outputs
        self.strict = strict
        self.calls: list[list[str]] = []

    def parse(self, docs: list[InputDoc], usage: Usage) -> list[dict[str, Any] | None]:
        self.calls.append([d.ref for d in docs])
        usage.api_calls += 1
        usage.input_tokens += 100 * len(docs)
        usage.output_tokens += 50 * len(docs)
        usage.cost_usd += 0.0001 * len(docs)
        out = []
        for d in docs:
            key = d.text()
            if key not in self.outputs and self.strict:
                raise AssertionError(f"no recorded output for {d.ref}: {key[:80]!r}")
            out.append(self.outputs.get(key, {"account": "other", "facts": []}))
        return out


def load_recorded(path: str) -> dict[str, Any]:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


@pytest.fixture
def recorded_posts() -> dict[str, Any]:
    return load_recorded("x_parse/posts.json")
