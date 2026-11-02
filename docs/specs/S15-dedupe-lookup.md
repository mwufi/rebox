# S15 — Dedupe pass + handle lookups for queued accounts

**Goal:** (a) the graph cleans itself: entities of the same kind that share a hard identity key get merged automatically (and only when it is safe), and likely duplicates go to the review queue; (b) accounts queued by handle (from the coverage checker, `@`-keyed frontier rows) get looked up and fetched by the X spider.

**Branch:** `s15-dedupe`  **DB prefix:** `s15_`  **Contract:** CONTRACTS §5 (resolver, review items), §7 admin merge (`POST /v0/admin/merge`), §12 workers/runs, §13 frontier.

## Owns
`apps/worker/rebox_worker/dedupe.py`, `apps/worker/tests/test_dedupe.py`, the `@`-key handling in `apps/worker/rebox_worker/connectors/x_spider.py` (+ its tests), `docs/DEDUPE.md`. Minimal `cli.py` registration lines (other branches also add commands there — keep it to a few lines). Do not change the writer, resolver or normalizers (another branch, S12, is hardening those).

## Why duplicates exist
The resolver matches on hard keys, but a key can arrive *after* minting: e.g. Exa mints "Figma" from a name+domain-less article, Wikidata later mints "Figma" keyed by QID and adds `domain: figma.com`; a second source later adds `domain: figma.com` to the first. Now two entities share `figma.com`. Also seen: OpenAI (two entities by `openai.com`), "Cursor" (x_ai) vs "Anysphere, Inc." (wikidata).

## (a) `rebox-worker dedupe [--dry-run] [--kinds company,person] [--limit N]`
1. **Find groups:** among un-merged entities of the same kind, group by identity predicate + `value_text` over **public** layers (identity predicates: `wikidata, domain, github, x_handle, x_id, hn_user`). One SQL per predicate as `rebox_app` with public layers visible; use the `claim_value_text_idx`.
2. **Decide per group** (union groups that share members first):
   - **Auto-merge** when no pair in the group has *conflicting* strong keys: two different non-null values of `wikidata`, or of `x_id`, or of `github` (for persons) means they are different things → don't auto-merge. `domain` alone conflicting is OK (companies have several domains) but `x_handle` conflicting for persons is a conflict.
   - Otherwise open a `review_item` (`kind='duplicate'`, payload: entity ids, shared key, conflicting keys) unless an open one for the same set exists.
3. **Canonical target:** the entity with a `wikidata` claim; else the one with most claims; tie → oldest `created_at`. Merge others into it via the **admin API** `POST /v0/admin/merge {from, into}` (ADMIN_TOKEN), so `merge_log` and `change` are written by the one place that owns merges. Never write `entity.merged_into` directly.
4. **Name-only candidates** (no shared key): same kind, folded-name equal (ignoring legal forms Inc/LLC/Corp and "(alias)" parentheticals), and at least one shared neighbor (an edge to the same entity, `follows` excluded) → review item `kind='duplicate'` with reason `name+neighbor`. Never auto-merge on names.
5. Record a run (`kind='enrich'`, worker `dedupe-enricher-<6 digits>`, kind `enricher`, registered like other workers) with stats `{groups, auto_merged, review_opened, skipped_conflict}`.
6. `--dry-run` prints groups, the decision and the canonical target per group, writes nothing.
7. Idempotent: a second run finds nothing to merge.

## (b) `@handle` frontier rows in the X spider
Frontier rows with `source='x_spider'` and key `@<handle>` (status `new`) are resolved in batches of up to 100 via `GET /2/users/by?usernames=...` (same user fields as following pages), each found account becomes a normal `x_user` RawRecord + a frontier row keyed by user id (depth 0, `discovered_by` carried over), and the `@` row is marked `fetched` (or `failed` with reason when the user doesn't exist / is suspended). This runs as a phase named `lookup` (added to `X_SPIDER_PHASES` parsing; default phases include it **first**). It counts against the existing spend guard (`X_TOTAL_BUDGET_USD`, user reads at the configured unit price) and stops cleanly at the budget.

## Verification
- Tests (offline, recorded/fake HTTP for X): auto-merge on shared domain; no auto-merge with conflicting wikidata (review item instead); person with conflicting x_handle → review; canonical choice rules; name+neighbor → review; dry-run writes nothing; second run is a no-op; merges go through the admin API (assert merge_log + change rows); `@` lookup phase: found/missing/suspended handling, budget stop, idempotency.
- Live (read/write on a copy only): `createdb` a copy of `rebox_live` as `s15_live` (if `-T` fails due to sessions, use `pg_dump rebox_live | psql s15_live`; **never** connect to `rebox_live` itself). Run an API from your worktree against `s15_live` on port 3315 for the admin merge. Report `--dry-run` output (group counts per predicate, 10 example decisions incl. Figma and OpenAI), then a real run's stats and before/after entity counts per kind. No live X calls (the lookup phase is tested offline only).
- `docs/DEDUPE.md`: what gets merged automatically vs reviewed, how to undo (`POST /v0/admin/split`), how to run in compose.
- Secrets: none needed. Never print/log/commit secrets.
