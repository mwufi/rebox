# ruff: noqa: E501  (inline synthetic payloads read better on one line)
"""Parser eval: run the model over ``eval_set.CASES``, score extracted facts against the labels.

    uv run python tests/parsers/eval_parser.py [--record] [--batch N] [--model M]

Live: needs ANTHROPIC_API_KEY. Prints precision / recall / F1 per field and overall, model usage
and cost per 1,000 inputs. ``--record`` writes the outputs to ``fixtures/raw/x_parse/eval/`` so
the offline test ``test_eval_recorded.py`` replays them (no network).

Scoring is on what ``extract`` emits (after the evidence, privacy and grounding guards), so it
measures the whole parser, not just the model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from eval_set import CASES  # noqa: E402

from rebox_worker.models import ClaimInput, RawRecord  # noqa: E402
from rebox_worker.parsers.extract import extract_claims  # noqa: E402
from rebox_worker.parsers.inputs import doc_for  # noqa: E402
from rebox_worker.parsers.llm import DEFAULT_MODEL, AnthropicModel, Usage  # noqa: E402
from rebox_worker.parsers.prompt import PROMPT_VERSION, InputDoc  # noqa: E402

REPO = HERE.parents[3]
RECORDED = REPO / "fixtures" / "raw" / "x_parse" / "eval" / "outputs.json"
FETCHED_AT = datetime(2026, 10, 25, tzinfo=UTC)
FIELDS = ("worked_at:current", "worked_at:past", "founded", "based_in", "milestone", "acquired", "hire")


def docs_for_cases() -> list[tuple[str, str | None, InputDoc]]:
    out = []
    for cid, source, kind, payload, _ in CASES:
        ext = f"eval:{cid}"
        rec = RawRecord.new(source, ext, payload, fetched_at=FETCHED_AT)
        doc = doc_for(rec)
        assert doc is not None, cid
        out.append((cid, kind, doc))
    return out


def payload_for(doc: InputDoc, output: dict[str, Any] | None, account_kind: str | None) -> dict[str, Any]:
    graph: dict[str, Any] = {}
    if account_kind and doc.meta.get("handle"):
        key = "github" if doc.kind == "github_bio" else "x_handle"
        graph[f"{key}:{str(doc.meta['handle']).lower()}"] = [account_kind]
    return {
        "input_ref": doc.ref,
        "input_hash": "",
        "model": "eval",
        "prompt_version": PROMPT_VERSION,
        "input": {"kind": doc.kind, "fields": doc.fields, "meta": doc.meta},
        "output": output,
        "graph": graph,
    }


def _label(ref: Any, own: InputDoc) -> str:
    """How a fact tuple names an entity: @handle, a lowercase name, a city, or ``self``."""
    if ref is None:
        return ""
    ident = ref.identity or {}
    own_handle = str(own.meta.get("handle") or "").lower()
    for k in ("x_handle", "github"):
        if ident.get(k):
            h = str(ident[k]).lower()
            if h == own_handle and own.kind != "article":
                return "self"
            return "@" + h
    if ident.get("x_id") and str(ident["x_id"]) == str(own.meta.get("x_id")):
        return "self"
    if ident.get("wikidata"):
        return ref.name or str(ident["wikidata"])
    return (ref.name or "").lower()


def fact_tuples(claims: list[ClaimInput], doc: InputDoc) -> set[tuple[str, ...]]:
    out: set[tuple[str, ...]] = set()
    places = {}
    from rebox_worker.parsers.places import CITIES

    places = {q: c for c, q in CITIES.items()}
    for c in claims:
        p = c.predicate
        if p == "worked_at":
            subj = _label(c.subject, doc)
            obj = _label(c.object, doc)
            if doc.kind in ("x_post", "article"):  # posts only produce dated hires
                who = "@" + str(doc.meta.get("handle") or "").lower() if subj == "self" else subj
                out.add(("hire", who, obj))
                continue
            title = (c.value or {}).get("title", "") if isinstance(c.value, dict) else ""
            out.add(("worked_at", obj, title, "past" if c.valid_to else "current"))
        elif p == "founded":
            out.add(("founded", _label(c.object, doc)))
        elif p == "based_in":
            q = str((c.object.identity or {}).get("wikidata"))
            out.add(("based_in", places.get(q, q)))
        elif p == "milestone":
            if doc.kind != "article" and _label(c.subject, doc) == "self" and c.subject.kind == "person":
                # person-attached copy of a first-person milestone: score the kind once per post
                out.add(("milestone", "self", c.value["kind"]))
            else:
                out.add(("milestone", _label(c.subject, doc), c.value["kind"]))
        elif p == "acquired":
            out.add(("acquired", _label(c.subject, doc), _label(c.object, doc)))
    return out


def field_of(t: tuple[str, ...]) -> str:
    return f"worked_at:{t[3]}" if t[0] == "worked_at" else t[0]


def lenient(t: tuple[str, ...]) -> tuple[str, ...]:
    """Role titles are free text: score worked_at on (org, status); titles are reported apart."""
    return (t[0], t[1], t[3]) if t[0] == "worked_at" else t


def score(results: list[tuple[str, set, set]]) -> dict[str, Any]:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    title_ok = title_n = 0
    errors = []
    for cid, got, want in results:
        g = {lenient(t): t for t in got}
        w = {lenient(t): t for t in want}
        for k, t in g.items():
            if k in w:
                tp[field_of(t)] += 1
                if t[0] == "worked_at":
                    title_n += 1
                    title_ok += int(t[2].casefold() == w[k][2].casefold())
            else:
                fp[field_of(t)] += 1
                errors.append((cid, "FP", t))
        for k, t in w.items():
            if k not in g:
                fn[field_of(t)] += 1
                errors.append((cid, "FN", t))
    rows = {}
    for f in (*FIELDS, "all"):
        a, b, c = (
            (sum(tp.values()), sum(fp.values()), sum(fn.values())) if f == "all" else (tp[f], fp[f], fn[f])
        )
        prec = a / (a + b) if a + b else 1.0
        rec = a / (a + c) if a + c else 1.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        rows[f] = {
            "tp": a,
            "fp": b,
            "fn": c,
            "precision": round(prec, 3),
            "recall": round(rec, 3),
            "f1": round(f1, 3),
        }
    empty = [cid for cid, got, want in results if not want]
    rows["no_fact_inputs_clean"] = (
        f"{sum(1 for cid, got, want in results if not want and not got)}/{len(empty)}"
    )
    rows["title_exact"] = f"{title_ok}/{title_n}"
    return {"fields": rows, "errors": errors}


def run(outputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list]]:
    results = []
    claims_by_case = {}
    for (cid, kind, doc), case in zip(docs_for_cases(), CASES, strict=True):
        claims = extract_claims("x_parse", payload_for(doc, outputs.get(cid), kind))
        claims_by_case[cid] = claims
        results.append((cid, fact_tuples(claims, doc), case[4]))
    return score(results), claims_by_case


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--model", default=os.environ.get("PARSER_MODEL") or DEFAULT_MODEL)
    ap.add_argument("--replay", action="store_true", help="score the recorded outputs (no network)")
    args = ap.parse_args()

    docs = docs_for_cases()
    usage = Usage()
    if args.replay:
        outputs = json.loads(RECORDED.read_text())["outputs"]
    else:
        model = AnthropicModel(os.environ["ANTHROPIC_API_KEY"], name=args.model)
        outputs = {}
        # Group by input kind, as the connectors do (bios with bios, posts with posts).
        by_kind: dict[str, list[tuple[str, InputDoc]]] = {}
        for cid, _, doc in docs:
            by_kind.setdefault(doc.kind, []).append((cid, doc))
        for kind, items in by_kind.items():
            size = 4 if kind == "article" else args.batch
            for i in range(0, len(items), size):
                chunk = items[i : i + size]
                outs = model.parse([d for _, d in chunk], usage)
                outputs.update({cid: o for (cid, _), o in zip(chunk, outs, strict=True)})
    report, _ = run(outputs)
    for k, v in report["fields"].items():
        print(f"{k:20s} {v}")
    for e in report["errors"]:
        print("  ", *e)
    if not args.replay:
        u = usage.as_dict()
        print("usage", json.dumps(u))
        print(
            f"cost per 1,000 inputs: ${u['cost_usd'] / len(docs) * 1000:.4f}  (batch {args.batch}, model {args.model})"
        )
        if args.record:
            RECORDED.parent.mkdir(parents=True, exist_ok=True)
            RECORDED.write_text(
                json.dumps(
                    {
                        "_fixture": "Recorded model outputs for the synthetic eval set (tests/parsers/eval_set.py).",
                        "model": args.model,
                        "prompt_version": PROMPT_VERSION,
                        "batch": args.batch,
                        "usage": u,
                        "outputs": outputs,
                    },
                    indent=1,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            print("recorded", RECORDED)
    return 0


if __name__ == "__main__":
    sys.exit(main())
