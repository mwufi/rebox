# S17 — Source trust + legacy claim workers

**Goal:** the fold picks the right value when sources disagree. Today claims written before migration 0005 (all of `public/wikidata`, `public/hn`, `public/github` on older instances) have no worker and fold at a fallback trust of 0.5, so X display names beat Wikidata names ("vitalik.eth" over "Vitalik Buterin", "Perplexity" over "Perplexity AI"). Fix the data and make trust per source configurable.

**Branch:** `s17-trust`  **DB prefix:** `s17_`  **Contract:** CONTRACTS §12 (workers, default trust, fold tie-break).

## Owns
`db/migrations/0008_legacy_workers.sql`, `apps/worker/rebox_worker/identity.py` (trust by source), `apps/worker/rebox_worker/client.py` (`register_worker(..., id=None, trust=None)` public params), `apps/worker/rebox_worker/exchange.py` (switch to the public `register_worker(id=...)` and to `client.write(..., drop_rejected=True)`), `config/trust.yaml` (new), tests for each, CONTRACTS §12 wording proposal (in the report).

## Build
1. **Source trust table** `config/trust.yaml`: `defaults` by worker kind (unchanged from §12: connector 0.8, spider 0.8, parser 0.6, enricher 0.5, external 0.3) and `sources` overrides: `wikidata: 0.9` (curated), `github: 0.8`, `hn: 0.7` (names are often handles), `x_spider: 0.75` (display names are self-chosen), `exa: 0.7`, parsers keep 0.6. Loaded by `identity.py`; the registered trust for a new worker = source override if present else kind default. Existing workers whose trust still equals their kind default get PATCHed to the table value on next registration (`PATCH /v0/admin/workers/:id` needs ADMIN_TOKEN — if the worker has no admin token, log a one-line hint and continue). Workers whose trust an admin changed are never touched (compare against the kind default; record `config.trust_source='table'` when the table set it).
2. **Migration 0008 (owner):** for every layer with claims where `worker_id is null`, create a worker `<source>-connector-000000` (kind `connector`, source from the layer id, owner `rebox`, layers `{layer}`, trust from the same table — inline the numbers in SQL with a comment pointing at `config/trust.yaml`, description "claims written before worker identities (migration 0008)") unless it exists, then set `claim.worker_id` for those claims. Do it without emitting 30k change rows: `alter table claim disable trigger rebox_claim_change` … `enable` inside the migration (this is a provenance backfill, not a fact change). Must be idempotent and fast on 50k claims; works on Zen's instance too (unknown layer set — derive from data).
3. **Fold:** confirm the API fold no longer needs the 0.5 fallback for normal data (keep it for safety). No fold logic change otherwise.
4. **client.py:** `register_worker(..., id: str | None = None, trust: float | None = None)` documented; `exchange.py` uses it instead of `_request`, and uses `write(..., drop_rejected=True)` (S12) instead of its own per-chunk filter, keeping its tests green.

## Verification
- worker suite + api suite green (`S2_OWNER_URL/S2_APP_URL` → `s17_pytest`; `S1_TEST_DB=s1_s17`), ruff clean, api typecheck.
- Migration tests: on a DB with null-worker claims in 3 layers → 3 legacy workers, all claims attributed, 0 new change rows, second run is a no-op.
- Live on a copy only (`createdb s17_live && pg_dump rebox_live | psql -q s17_live`; **never** write to `rebox_live`): apply 0008, run an API from your worktree on port 3317, and show folded names before/after for Vitalik Buterin, Perplexity, Mercury, Loops, Marc Andreessen, plus counts of entities whose folded `name` changed by source pair (x_spider→wikidata etc.).
- Secrets: none needed; never print/log/commit secrets.
