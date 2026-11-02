# ruff: noqa: E501  (hand-written test data: one case per line)
"""Record model outputs for the offline parser tests (live; run by hand, needs ANTHROPIC_API_KEY).

    uv run python tests/parsers/record_fixtures.py

Writes ``fixtures/raw/x_parse/{bios,posts}.json`` and ``fixtures/raw/exa_parse/articles.json``:
``{"inputs": [upstream raw payload...], "outputs": {input text: model output}}``. All inputs are
synthetic (invented people, handles and companies; handles end in ``_ex``).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from eval_set import article, bio, gh, post  # noqa: E402

from rebox_worker.models import RawRecord  # noqa: E402
from rebox_worker.parsers.inputs import doc_for  # noqa: E402
from rebox_worker.parsers.llm import AnthropicModel, Usage  # noqa: E402
from rebox_worker.parsers.prompt import PROMPT_VERSION  # noqa: E402

FIXTURES = HERE.parents[3] / "fixtures" / "raw"
D = "2026-10-21T17:30:00.000Z"

BIOS: list[tuple[str, dict[str, Any]]] = [
    (
        "x_spider",
        bio(
            "zoe_founder_ex",
            "Zoe Hart",
            "co-founder & CEO @tallyhq_ex (YC S24). ex-@stripe_ex, prev PM at Plaidly",
            "San Francisco, CA",
            "9000000001",
        ),
    ),
    (
        "x_spider",
        bio(
            "marc_vc_ex",
            "Marc Bell",
            "investing at @harborvc_ex. formerly eng @looptech_ex",
            "SF",
            "9000000002",
        ),
    ),
    (
        "x_spider",
        bio(
            "ivy_ml_ex",
            "Ivy Tran",
            "ML eng @tallyhq_ex · DM for collabs · call 415-555-0199",
            "Bay Area",
            "9000000003",
        ),
    ),
    (
        "x_spider",
        bio(
            "tallyhq_ex",
            "Tally",
            "Tally is the AI controller for startups. Backed by @harborvc_ex.",
            "San Francisco",
            "9000000004",
        ),
    ),
    ("x_spider", bio("nobody_ex", "N.", "just vibes ✨ she/her", "🌍", "9000000005")),
    (
        "x_spider",
        bio(
            "hugo_ex",
            "Hugo Lam",
            "Building @fernhill_ex. Previously founded Bramble (acq. by Shopwise). NYC → SF",
            "",
            "9000000006",
        ),
    ),
    (
        "github",
        gh("octo-ex-9", "Rosa Diaz", "Staff engineer. Previously at Heroku.", "Vercel", "San Francisco"),
    ),
]

POSTS: list[tuple[str, dict[str, Any]]] = [
    (
        "x_spider",
        post(
            "tallyhq_ex",
            "Today we're launching Tally Close: month-end close in a day, not a week. Live for every customer now.",
            D,
            "1800000000000000001",
        ),
    ),
    (
        "x_spider",
        post(
            "tallyhq_ex",
            "We raised a $9M seed led by @harborvc_ex to build the AI controller for startups 🚀",
            D,
            "1800000000000000002",
        ),
    ),
    (
        "x_spider",
        post(
            "tallyhq_ex",
            "Welcome @ivy_ml_ex to @tallyhq_ex as our first ML engineer!",
            D,
            "1800000000000000003",
        ),
    ),
    (
        "x_spider",
        post(
            "zoe_founder_ex",
            "lol our office plant has more uptime than our staging env",
            D,
            "1800000000000000004",
        ),
    ),
    (
        "x_spider",
        post(
            "tallyhq_ex",
            "We're hiring founding engineers in SF! Apply via the link in bio.",
            D,
            "1800000000000000005",
        ),
    ),
    (
        "x_spider",
        post(
            "fernhill_ex",
            "Fernhill has acquired @sprocket_ex. Their team is joining us to build the future of supply chains.",
            D,
            "1800000000000000006",
        ),
    ),
    (
        "x_spider",
        post("marc_vc_ex", "Personal news: I'm joining @harborvc_ex as a Partner!", D, "1800000000000000007"),
    ),
    (
        "x_spider",
        post(
            "tallyhq_ex",
            "Tally 2.0 is out: multi-entity books, audit trails and a brand new API.",
            D,
            "1800000000000000008",
        ),
    ),
    (
        "x_spider",
        post(
            "zoe_founder_ex",
            "Unpopular opinion: most startups should not raise a seed round. Thread 🧵",
            D,
            "1800000000000000009",
        ),
    ),
    (
        "x_spider",
        post(
            "tallyhq_ex",
            "Tally won Fintech Startup of the Year at the Bay Fintech Awards! 🏆",
            D,
            "1800000000000000010",
        ),
    ),
    (
        "x_spider",
        post(
            "tallyhq_ex",
            "Questions about billing? Call our team at +1 415 555 0100 any weekday.",
            D,
            "1800000000000000011",
        ),
    ),
    (
        "x_spider",
        post(
            "harborvc_ex",
            "Proud to lead the $9M seed for @tallyhq_ex. Welcome to the Harbor family, @zoe_founder_ex!",
            D,
            "1800000000000000012",
        ),
    ),
]

ARTICLES: list[tuple[str, dict[str, Any]]] = [
    (
        "exa",
        article(
            "https://news.example/2026/10/tally-seed",
            "Tally raises $9M seed for AI accounting",
            "San Francisco-based Tally has raised a $9 million seed round led by Harbor Ventures. The startup, "
            "founded by Zoe Hart, automates month-end close for startups.",
            "2026-10-21T00:00:00.000Z",
        ),
    ),
    (
        "exa",
        article(
            "https://news.example/2026/10/fernhill-sprocket",
            "Fernhill buys Sprocket",
            "Supply-chain software maker Fernhill has acquired Sprocket, a logistics API startup, for an "
            "undisclosed amount.",
            "2026-10-19T00:00:00.000Z",
        ),
    ),
    (
        "exa",
        article(
            "https://essays.example/agents",
            "On agents",
            "I think agents are the new apps. Here is why.",
            "2026-10-18T00:00:00.000Z",
        ),
    ),
]


def record(
    cases: list[tuple[str, dict[str, Any]]], model: AnthropicModel, usage: Usage, batch: int
) -> dict[str, Any]:
    docs = []
    for i, (source, payload) in enumerate(cases):
        doc = doc_for(
            RawRecord.new(source, f"fx:{i}", payload, fetched_at=datetime(2026, 10, 25, tzinfo=UTC))
        )
        assert doc is not None, payload
        docs.append(doc)
    outputs: dict[str, Any] = {}
    for i in range(0, len(docs), batch):
        chunk = docs[i : i + batch]
        for d, o in zip(chunk, model.parse(chunk, usage), strict=True):
            outputs[d.text()] = o
    return {"inputs": [{"source": s, "payload": p} for s, p in cases], "outputs": outputs}


def main() -> int:
    model = AnthropicModel(
        os.environ["ANTHROPIC_API_KEY"], name=os.environ.get("PARSER_MODEL") or "claude-haiku-5-5"
    )
    usage = Usage()
    for path, cases, batch in (
        ("x_parse/bios.json", BIOS, 10),
        ("x_parse/posts.json", POSTS, 8),
        ("exa_parse/articles.json", ARTICLES, 4),
    ):
        data = record(cases, model, usage, batch)
        out = FIXTURES / path
        out.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "_fixture": "Synthetic inputs (invented people and companies) and recorded model outputs.",
            "model": model.name,
            "prompt_version": PROMPT_VERSION,
            **data,
        }
        out.write_text(json.dumps(body, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        print("wrote", out)
    print("usage", json.dumps(usage.as_dict()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
