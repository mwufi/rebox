# S16 — Resolver convergence + raw-index repair

**Goal:** stop creating the duplicates S15 cleans up, and make an aborted run recoverable without hand-written scripts.

**Branch:** `s16-resolver`  **DB prefix:** `s16_`  **Contract:** CONTRACTS §3 ids, §5 resolver, §6 writer errors.

## Owns
`apps/worker/rebox_worker/{resolve.py,ids.py,names.py (new),rawstore.py}`, CLI registration for `raw reindex`, their tests; `apps/api/src/writer*.ts` (error attribution only) + tests; CONTRACTS §3/§5/§6 wording (propose exact text in the report; the architect edits the contract).

## Findings this fixes (from the live graph)
1. Bios name companies without an @handle ("SWE at Google"). These refs have no identity keys, so `_mint` scopes the mint to the record: each parser batch mints its own "google". Result: 1,500+ name-only companies, many duplicating each other and keyed companies.
2. `x_id` is an identity predicate but not in `ids.IDENTITY_KEY_ORDER`, so a ref whose only identity is `x_id` gets an identity-less mint and all such refs in one record collapse into one entity.
3. When a run aborts after `raw.put(..., index=False)`-style writes, files exist in the raw store but not in `raw_record`; `replay` iterates the index and silently skips them (200 X accounts were missed until the architect reindexed by hand).
4. The writer reports DB-level rejections (`mapDbError` "rejected by database") with index -1, which fails the whole batch.

## Build
1. **`names.py`:** `fold_name(kind, name)` — NFKC, casefold, strip accents, trim, collapse whitespace; for companies also strip one trailing "(...)" parenthetical, a leading "the ", and legal suffixes (`inc, inc., llc, l.l.c., ltd, ltd., corp, corp., corporation, co, co., gmbh, s.a., sa, plc, pbc`), and trailing punctuation. Pure; table-driven tests.
2. **Unscoped company name pass** in the resolver, after hard keys and scoped fuzzy, before mint, **for `company` refs with no identity keys only**: look up un-merged companies whose `name` (public layers + the writer's visible layers) folds equal to the ref's folded name.
   - exactly one with any identity key → match it (decision `matched`, pass `company_name`);
   - several keyed → match the oldest **and** open a `possible_duplicate` review item (once per name set);
   - none keyed but name-only ones exist → match the one with the most claims (tie oldest);
   - none → mint **deterministically from the folded name**: `mint_id(kind, "name", folded)` so later mentions converge. Add `name` as the last entry of the key order *for this purpose only* (a real `name` claim is never treated as a hard key elsewhere).
   Use `search_name` (trigram GIN) to fetch candidates cheaply (`name % $1` is not usable under RLS on `claim`; `search_name` has no RLS) and compare folded names in Python. Batch per resolve() call. Persons are never matched by name here.
3. **`x_id`** goes into `IDENTITY_KEY_ORDER` right after `x_handle` (ids minted from stronger keys are unchanged; only x_id-only refs change, which were wrong). Test the collapse case.
4. **`rebox-worker raw reindex SOURCE [--dry-run]`:** walk the raw store for `SOURCE` (file: rglob; s3: list_objects_v2 under the prefix), and index every record whose `(source, external_id)` is missing from `raw_record` or whose indexed `fetched_at` is older. Print counts. Idempotent.
5. **Writer:** attribute DB-level per-claim errors to their index when the failing claim can be identified (e.g. write claims one-by-one inside a savepoint on a batch DB error, or validate the failing constraint up front); keep index -1 only for truly batch-level errors (auth, layer, run). The worker client already drops by index (S12).

## Verification
- worker: unit tests for `fold_name`; resolver tests: two records naming "Google" → one entity; "Google" with an existing wikidata-keyed "Google LLC" → match; two keyed "Notion" → oldest + review item; persons never name-matched; x_id-only refs in one record → distinct entities; reindex on a store with unindexed files (file store; s3 via the existing fake client) + idempotency; full suite green (`S2_OWNER_URL/S2_APP_URL` → `s16_pytest`, see S14/S15 for the conftest workaround), ruff clean.
- api: writer test where one claim in a batch of 50 hits a DB constraint → 400 with that index; typecheck/tests green (`S1_TEST_DB=s1_s16`).
- Live (copy only): copy `rebox_live` → `s16_live` (`pg_dump rebox_live | psql s16_live`; **never** write to `rebox_live`), run an API from your worktree on port 3316, then `replay x_parse` (no model calls — replay reads stored model output) into it and report: companies before/after, how many name-only refs matched a keyed company, examples (google, palantir, notion, stripe, ramp), review items opened.
- Secrets: none needed; never print/log/commit secrets.

Note: S15 (dedupe, in flight) also folds company names for its convergence rule. Put yours in `names.py`; the architect will point S15's at it after both merge.
