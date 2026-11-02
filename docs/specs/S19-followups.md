# S19 — Follow-ups from the overnight run

**Goal:** close the small gaps the S11–S18 agents reported, so the next run on Zen's machine is reproducible with commands rather than scratch scripts.

**Branch:** `s19-followups`  **DB prefix:** `s19_`

## Owns
`apps/worker/rebox_worker/{exchange.py,pipeline.py,cli.py,parsers/extract.py}`, `apps/api/src/routes/workers.ts` (PATCH config only), their tests, `docs/{MERGE.md,PARSING.md,RUNBOOK.md}`.

## Build
1. **Import trust.** When `import` registers a worker that is new on the target, register it with the target's table trust (`identity.trust_for(kind, source)`) and the `config.trust_source='table'` marker, so imported spiders get 0.75/0.7 like local ones. Workers that already exist keep their trust (report the difference as today).
2. **PATCH config merge.** `PATCH /v0/admin/workers/:id` accepts `config` (a JSON object merged shallowly into the stored config). The worker client's table-trust code (S17) then writes the `trust_source`/`trust_table` marker when it applies the table to an existing worker.
3. **`replay --retract-stale`.** After a replay of a source, for each worker of that source, retract (write `retract: true` claims as that worker, in a `replay` run) every non-retracted claim in the source's layers whose `source_ref` the replay did not emit. Guardrails: only claims written by that source's workers; `--dry-run` prints counts per predicate and 10 examples; refuses if it would retract more than 30% of the layer unless `--force`. This replaces the architect's scratch script (`retract_stale.py`, attached as reference in the spec folder: see below).
4. **Parser identity claims for domains and Wikidata places.** Same bug class as the @handle fix (S18): parsers mint companies from an article `domain` and places from a `wikidata` QID without writing that identity claim. Emit `domain` / `wikidata` identity claims for entities a parser mints from those keys (not for keys already known in the graph at fetch time), with the referencing evidence. Pure extract change; `replay` applies it.
5. **Docs.** MERGE.md: the exact OrbStack sequence (below) including "pull main, rebuild, migrate first". RUNBOOK.md: an "after a crash" section (`raw reindex`, `replay`, `replay --retract-stale`).

Reference for 3 (the scratch logic used on the live graph): collect `source_ref`s the current extract emits for the stored raw records; claims in the layer with other `source_ref`s are stale; write them back with `retract: true` as the original worker in a new run.

## Verification
- worker suite (`S2_OWNER_URL/S2_APP_URL` → `s19_pytest`, `S2_TEST_DB=s2_s19`) + ruff; api typecheck + tests (`S1_TEST_DB=s1_s19`).
- Tests for each item (import registers table trust; PATCH config merge; retract-stale incl. dry-run, 30% guard, other workers untouched, idempotent; parser emits domain/wikidata identity claims, replay idempotent).
- Live on a copy only (`createdb s19_live && pg_dump rebox_live | psql -q s19_live`; never write to `rebox_live`; API on 3319): `replay x_parse` + `replay exa_parse` with no ANTHROPIC_API_KEY set, `--retract-stale --dry-run` counts (expect ~0 for exa_parse — the architect already retracted the 46 stale milestones), identity claims added by item 4, and an import of `/home/claude/rebox/live/export/rebox-overnight-2026-11-02.rebox.jsonl.gz` into a fresh `createdb -T s4_live s19_target` (migrated) showing table trust on the new workers.
- Secrets: none needed; never print/log/commit secrets.
