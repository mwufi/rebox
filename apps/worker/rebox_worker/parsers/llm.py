"""The model client shared by every parser: a batch of ``InputDoc`` -> one output per doc.

``AnthropicModel`` calls the Messages API with one strict tool (``schema.TOOL``) that the model
is forced to call, so every answer is schema-valid JSON. The tool schema and the system prompt
form one cached prefix (``cache_control`` on the system block): every call of a run after the
first reads it from the cache at a tenth of the input price.

A batch that comes back truncated (``max_tokens``) or without a tool call is split in half and
retried; a single input that still fails is returned as ``None`` (the parser records it as an
error and moves on). Transport errors and 429/5xx are retried by the SDK (``max_retries``);
anything else propagates and fails the run (the cursor stays, the next run retries).

``Usage`` adds up calls, tokens and cost; prices are USD per million tokens (Haiku 5.5,
prompts <= 100k tokens; https://platform.claude.com/docs/en/about-claude/pricing).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, InputDoc, render
from .schema import TOOL, TOOL_NAME

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-5-5"
MODEL_ENV = "PARSER_MODEL"
KEY_ENV = "ANTHROPIC_API_KEY"

# USD per million tokens: input, cache write (5 min), cache read, output.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.125, 0.01, 0.50),
    "claude-haiku-4-5": (1.00, 1.25, 0.10, 5.00),
    "claude-sonnet-5-5": (2.00, 2.50, 0.10, 10.00),
}


@dataclass
class Usage:
    api_calls: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    failed_inputs: int = 0

    def add_call(self, model: str, usage: Any) -> dict[str, int]:
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        p_in, p_cw, p_cr, p_out = PRICES.get(model, PRICES[DEFAULT_MODEL])
        self.api_calls += 1
        self.input_tokens += inp
        self.cache_write_tokens += cw
        self.cache_read_tokens += cr
        self.output_tokens += out
        self.cost_usd += (inp * p_in + cw * p_cw + cr * p_cr + out * p_out) / 1e6
        return {"input": inp, "cache_write": cw, "cache_read": cr, "output": out}

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = round(self.cost_usd, 6)
        return d


class Model(Protocol):
    """What a parser needs from a model. Tests use a recorded implementation (no network)."""

    name: str

    def parse(self, docs: list[InputDoc], usage: Usage) -> list[dict[str, Any] | None]:
        """One output (``{"account", "facts"}``) per doc, or None when the doc could not be parsed."""
        ...


def model_name(env: Mapping[str, str]) -> str:
    return (env.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL


class AnthropicModel:
    def __init__(
        self,
        api_key: str,
        *,
        name: str = DEFAULT_MODEL,
        client: Any = None,
        max_retries: int = 4,
        timeout_s: float = 90.0,
    ) -> None:
        self.name = name
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries, timeout=timeout_s)
        self.client = client

    def parse(self, docs: list[InputDoc], usage: Usage) -> list[dict[str, Any] | None]:
        if not docs:
            return []
        outputs = self._call(docs, usage)
        if outputs is not None:
            return outputs
        if len(docs) == 1:
            usage.failed_inputs += 1
            return [None]
        mid = len(docs) // 2
        log.info("parser: splitting a batch of %d after an incomplete answer", len(docs))
        return self.parse(docs[:mid], usage) + self.parse(docs[mid:], usage)

    def _call(self, docs: list[InputDoc], usage: Usage) -> list[dict[str, Any] | None] | None:
        import anthropic

        local = [(str(i + 1), d) for i, d in enumerate(docs)]
        try:
            resp = self.client.messages.create(
                model=self.name,
                max_tokens=max_tokens_for(docs),
                system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=[TOOL],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=[{"role": "user", "content": render(local)}],
                metadata={"user_id": f"rebox-parser-{PROMPT_VERSION}"},
            )
        except anthropic.BadRequestError as e:
            log.warning("parser: request rejected for %d inputs: %s", len(docs), e)
            return None
        usage.add_call(self.name, resp.usage)
        if resp.stop_reason == "max_tokens":
            return None
        tool_input = next(
            (b.input for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name == TOOL_NAME),
            None,
        )
        if not isinstance(tool_input, dict):
            return None
        by_id: dict[str, dict[str, Any]] = {}
        for r in tool_input.get("results") or []:
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                by_id.setdefault(r["id"].strip(), r)
        out: list[dict[str, Any] | None] = []
        missing = 0
        for local_id, _ in local:
            r = by_id.get(local_id)
            if r is None:
                missing += 1
                out.append(None)
            else:
                out.append({"account": r.get("account", "other"), "facts": r.get("facts") or []})
        if missing == len(local):
            return None
        if missing:
            usage.failed_inputs += missing
            log.warning("parser: %d of %d inputs missing from the answer", missing, len(local))
        return out


def max_tokens_for(docs: list[InputDoc]) -> int:
    """Room for the answer: ~250 tokens per short input, more for posts/articles."""
    per = {"article": 700, "x_post": 300}
    return min(8192, 300 + sum(per.get(d.kind, 250) for d in docs))
