"""The parser's structured output: one tool whose input is the extraction result.

The model answers by calling ``TOOL_NAME`` exactly once with ``{"results": [...]}``, one result
per input (matched by ``id``). The JSON schema is strict-mode compatible (every property
required, ``additionalProperties: false``, no length/number constraints; "absent" is ``""``),
so the API guarantees the shape. ``Output`` re-validates it in ``extract`` anyway: a
recorded output from an older prompt, or a hand-edited fixture, must never crash a replay.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TOOL_NAME = "record_facts"

MILESTONE_KINDS = ("launch", "funding", "hire", "acquisition", "product", "award", "other")
ACCOUNT_KINDS = ("person", "company", "other")


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_QUOTE = {
    "type": "string",
    "description": "Exact contiguous span copied verbatim from the input that states this fact.",
}
_HANDLE = {
    "type": "string",
    "description": "The @handle exactly as written in the input, without '@'; '' if the input has none.",
}

ROLE = _obj(
    {
        "type": {"type": "string", "enum": ["role"]},
        "org": {"type": "string", "description": "Organization name as written ('' if only a handle)."},
        "org_handle": _HANDLE,
        "title": {"type": "string", "description": "Job title as written ('' if none)."},
        "status": {"type": "string", "enum": ["current", "past"]},
        "founder": _BOOL,
        "investor": _BOOL,
        "quote": _QUOTE,
    }
)
LOCATION = _obj(
    {
        "type": {"type": "string", "enum": ["location"]},
        "place": {"type": "string", "description": "City in canonical English, e.g. 'San Francisco'."},
        "quote": _QUOTE,
    }
)
MILESTONE = _obj(
    {
        "type": {"type": "string", "enum": ["milestone"]},
        "kind": {"type": "string", "enum": list(MILESTONE_KINDS)},
        "title": {"type": "string", "description": "Short factual headline, at most 12 words."},
        "org": _STR,
        "org_handle": _HANDLE,
        "first_person": _BOOL,
        "quote": _QUOTE,
    }
)
ACQUISITION = _obj(
    {
        "type": {"type": "string", "enum": ["acquisition"]},
        "acquirer": _STR,
        "acquirer_handle": _HANDLE,
        "target": _STR,
        "target_handle": _HANDLE,
        "quote": _QUOTE,
    }
)
HIRE = _obj(
    {
        "type": {"type": "string", "enum": ["hire"]},
        "person_handle": _HANDLE,
        "org": _STR,
        "org_handle": _HANDLE,
        "title": _STR,
        "quote": _QUOTE,
    }
)

INPUT_SCHEMA: dict[str, Any] = _obj(
    {
        "results": {
            "type": "array",
            "items": _obj(
                {
                    "id": {"type": "string", "description": "The id attribute of the <input>."},
                    "account": {"type": "string", "enum": list(ACCOUNT_KINDS)},
                    "facts": {
                        "type": "array",
                        "items": {"anyOf": [ROLE, LOCATION, MILESTONE, ACQUISITION, HIRE]},
                    },
                }
            ),
        }
    }
)

TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Record the facts extracted from the inputs. Call once, with one result per input.",
    "input_schema": INPUT_SCHEMA,
    "strict": True,
}


# ── validation of what the model returned ─────────────────────────────────


class _Fact(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    quote: str = Field(min_length=1)


class Role(_Fact):
    type: Literal["role"]
    org: str = ""
    org_handle: str = ""
    title: str = ""
    status: Literal["current", "past"] = "current"
    founder: bool = False
    investor: bool = False


class Location(_Fact):
    type: Literal["location"]
    place: str = Field(min_length=1)


class Milestone(_Fact):
    type: Literal["milestone"]
    kind: str = "other"
    title: str = Field(min_length=1)
    org: str = ""
    org_handle: str = ""
    first_person: bool = False


class Acquisition(_Fact):
    type: Literal["acquisition"]
    acquirer: str = ""
    acquirer_handle: str = ""
    target: str = ""
    target_handle: str = ""


class Hire(_Fact):
    type: Literal["hire"]
    person_handle: str = Field(min_length=1)
    org: str = ""
    org_handle: str = ""
    title: str = ""


Fact = Annotated[Role | Location | Milestone | Acquisition | Hire, Field(discriminator="type")]


class Result(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    account: str = "other"
    facts: list[Any] = Field(default_factory=list)


class _FactBox(BaseModel):
    fact: Fact


def parse_facts(raw: Any) -> tuple[list[Role | Location | Milestone | Acquisition | Hire], int]:
    """Validate one result's ``facts``: (valid facts, number dropped as invalid)."""
    if not isinstance(raw, list):
        return [], 0
    out = []
    bad = 0
    for item in raw:
        try:
            fact = _FactBox(fact=item).fact
        except ValidationError:
            bad += 1
            continue
        if isinstance(fact, Milestone) and fact.kind.lower() not in MILESTONE_KINDS:
            fact.kind = "other"
        elif isinstance(fact, Milestone):
            fact.kind = fact.kind.lower()  # enum casing may drift (structured outputs docs)
        out.append(fact)
    return out, bad
