# S2 — Worker core (Python)

**Goal:** a `rebox-worker` package that runs the pipeline fetch → raw store → extract → resolve → claims writer for any connector, with a Postgres job queue, scheduler and CLI. Real connectors come in a later slice; this slice ships one tiny example connector to prove the pipeline.

**Branch:** `s2-worker-core`  **DB prefix:** `s2_`

## Owns
`apps/worker/**`, `fixtures/raw/example/**`.

## Stack
Python 3.13, uv (`pyproject.toml`, console script `rebox-worker`), psycopg 3 (+ psycopg_pool), httpx, pydantic v2, boto3 (S3 raw store), pytest. No ORM.

## Build

1. **Models** (`rebox_worker/models.py`): `RawRecord`, `CursorUpdate`, `EntityRef`, `ClaimInput`, resolved `ClaimWrite` matching CONTRACTS §5 and §8.
2. **Normalizers + ids** (`normalize.py`, `ids.py`): every normalizer in §4, name normalization, deterministic `mint_id(kind, identity, source, source_ref)` per §3. Table-driven unit tests with the §4 examples.
3. **Raw store** (`rawstore.py`): `RawStore.from_url(RAW_STORE)` with `FileRawStore` and `S3RawStore` (boto3; test S3 with moto or a stub client). Key layout per §8. `put` returns the key and records it in `raw_record`; skip unchanged `content_hash`. `iter(source)` yields stored records for replay.
4. **DB** (`db.py`): connection pool on `DATABASE_URL` (app role). Helper `with view(visible, writable)` calling `rebox_set_view` per transaction.
5. **Resolver** (`resolve.py`): exactly the three passes in §8 (hard keys → scoped fuzzy → deterministic mint), batch cache, `resolution_log` rows, `review_item` on ambiguity. Hard-key lookup uses identity predicates from the `predicate` table (`meta.identity = true`) and the `claim_value_idx` index. Converts `ClaimInput` → `ClaimWrite` (`{"id"}` refs for matched entities, `{"mint": {...}}` for new ones). Also adds a `name` claim for minted entities when the EntityRef carries a `name`.
6. **Writer client** (`client.py`): batches of ≤ 500 to `POST {API_URL}/internal/claims` with `INGEST_TOKEN`, retries with backoff on 5xx/network errors, raises on 400 with details.
7. **Pipeline** (`pipeline.py`): `run_sync(connector)` = load cursor → `fetch` → raw store → `extract` → resolve → write → save cursor after each successful write batch; `run_replay(connector)` = iterate raw store → extract → resolve → write. Stats (fetched, skipped_unchanged, claims, minted, matched, review) saved to `connector_state.last_stats`.
8. **Jobs + scheduler** (`jobs.py`): enqueue, claim (`FOR UPDATE SKIP LOCKED`), retry with exponential backoff up to `max_attempts`, mark done/failed. `schedule_due()` enqueues `sync` for connectors whose `next_run_at <= now()` (and no queued/running sync for that source), then advances `next_run_at` by `interval_s`.
9. **Connector registry** (`connectors/__init__.py`): name → class. Ship `connectors/example.py`: reads JSON files from `fixtures/raw/example/` (no network) describing two companies and three people with overlapping identity keys, so the pipeline exercises match + mint + fuzzy.
10. **CLI** (`cli.py`): `rebox-worker run` (loop: claim and run jobs; `--once`), `sync <source>` (run inline now), `replay <source>`, `schedule-due`, `enqueue <source>`, `status` (connector_state table).

## Verification
Use `scripts/test-db.sh s2_test` (owner URL for setup, app URL for the worker). For tests that need the writer API, run a **fake writer** in-process (a tiny HTTP server in the test fixture that validates the §5 shape and inserts via the app URL with `rebox_set_view`), since the real API is built in parallel.

- `cd apps/worker && uv run pytest -q` passes, covering: normalizers (§4 table), deterministic ids, raw store skip-unchanged (file + S3 stub), resolver hard-key match, ambiguous → review_item, scoped fuzzy match, mint, batch cache consistency, job claim concurrency (two workers never run the same job), retry/backoff, schedule_due idempotency, end-to-end `sync example` producing the expected entity/claim counts, and `replay example` producing zero new entities on second run.
- `uv run ruff check` clean (add ruff as a dev dependency).
- Paste the output of `rebox-worker sync example` and `rebox-worker status` against a test DB with the fake writer.
