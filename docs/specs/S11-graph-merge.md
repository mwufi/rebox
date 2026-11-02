# S11 — Merging graphs between Rebox instances (export → import through the resolver)

**Goal:** move whole layers from one Rebox instance to another (tonight: the overnight-built graph → Zen's local "production" graph) without wiping anything, keeping provenance, and **merging entities correctly** even when the two instances minted different ids for the same real thing.

**Branch:** `s11-merge`  **DB prefix:** `s11_`

## Owns
`apps/worker/rebox_worker/exchange.py` (export/import), CLI commands, `apps/worker/tests/test_exchange.py`, `docs/MERGE.md`. Minimal edits to `cli.py` registration.

## Why not copy ids
Ids are deterministic per strongest identity key, so the same company found via the same key gets the same id on both sides. But the same person can be minted from different keys on different instances (from `github` on Zen's machine, from `x_id` here). Copying ids would create duplicates. So import goes through **the target's resolver**, like any connector.

## Bundle format (`.rebox.jsonl.gz`)
One JSON object per line:
- `{"type": "header", "version": 1, "source_instance", "exported_at", "layers": [...], "workers": [{id, kind, source, trust, layers, config, description}]}`
- `{"type": "claim", "layer", "worker", "subject": EntityRef, "predicate", "object": EntityRef|null, "value", "valid_from", "valid_to", "retract", "source_ref", "evidence"}` where each EntityRef is the **unresolved** form `{kind, identity: {<identity predicate>: value, ...}, name, origin_id}` built from the exported entity's identity claims (all of them, across the exported layers) — so the target's resolver can match on any key it knows. `origin_id` is a hint only.

## Export
`rebox-worker export --layers public/x_spider,public/exa,... --out FILE` reads through the API's read model or the DB as `rebox_app` with public layers visible; includes every claim in those layers (not folded), their workers, and identity facts for every referenced entity (even if those facts live in other layers, include them as identity hints, not as claims). Deterministic order; gzip.

## Import
`rebox-worker import FILE [--dry-run] [--layers ...]`:
1. Register each worker from the header on the target with the **same id** (registration accepts an explicit id; idempotent), same kind/trust/layers; if the target already has that id with a different source, abort.
2. Stream claims in batches through the normal resolver (hard keys → scoped fuzzy → mint), grouped by worker, each worker in its own run of kind `replay` with stats; evidence preserved.
3. `--dry-run` prints what would happen: entities matched vs minted per kind, top examples of matches (origin id → target id, by which key), claims per layer, and conflicts (a `one` attribute with a different winning value on the target).
4. Idempotent: importing the same bundle twice writes nothing the second time.

## Verification
- Tests: round-trip export→import into an empty DB reproduces the same folded entity views; import into a DB where the same person exists under a different id (minted from `github`) with an overlapping `x_handle` merges onto the existing entity (no duplicate); re-import is a no-op; worker ids and evidence preserved; dry-run makes no writes.
- Live: export the layers built tonight from `rebox_live` (the architect will tell you which layers), import into a copy of a DB that simulates Zen's (copy `s4_live` and add a few GitHub-minted people who also appear in the X layer), and report dry-run output + final stats.
- `docs/MERGE.md`: how to run it on Zen's OrbStack stack (`docker compose cp` the bundle into the worker container, `docker compose exec worker rebox-worker import --dry-run ...`, then without `--dry-run`).
