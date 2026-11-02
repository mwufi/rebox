# S6 — Integration: real container stack, first run, polish, docs

**Goal:** `cp .env.example .env && docker compose up --build` on a fresh machine yields a working Rebox at http://localhost:3000 that fills itself with the public SF graph (Wikidata + HN) without manual steps, and the repo docs describe exactly that. Fix what the end-to-end run reveals.

**Branch:** `s6-integration`  **DB prefix:** `s6_`  **Depends on:** S1–S5 merged (they are).

## Owns
Anything needed for the items below, with the smallest change that fits. Do not change `docs/CONTRACTS.md` or `db/migrations/*`; if a schema change is needed, add a new migration `db/migrations/0005_*.sql` and explain it in the report.

## Items
1. **Real container stack.** Start `dockerd` (it is installed; see `docs/RUNBOOK.md` and `infra/docker/compose.extra-ca.yml` for the TLS-proxy overlay this sandbox needs; Docker Hub may rate-limit — `public.ecr.aws/docker/library/*` mirrors work). Build and run the full stack with the real apps. Fix Dockerfiles/compose until: migrate exits 0, api healthy, worker and scheduler running, explorer served at `/`, `/v0/stats` works. Use a non-default host port if 3000/54329 are taken here.
2. **First run fills the graph.** On a fresh stack, the scheduler must enqueue `hn` and `wikidata` (and skip disabled `github`/`x`) without anyone running a command; verify the worker completes both and `/v0/stats` shows thousands of entities. If `connector_state` rows only exist after a manual run, fix that (e.g. `schedule_due` registers enabled connectors with `next_run_at = now()`). Keep the `example` connector out of the automatic schedule (it is test data).
3. **Ctx cleanup** (contract questions from S4): `schedule_due` and the CLI pass `Ctx(env=os.environ)`; give `Ctx` a `db` handle so connectors (X) don't open their own connection. Update github/x connectors accordingly; tests stay green.
4. **UI polish from the architect's review of the live graph:**
   - Point-in-time edges (`launched`, `posted`, `attended`) show one date ("Jun 2018"), not a range ending in "now". Decide per predicate via `predicate.meta` hints only if needed; a small list in the web app is fine for v0.
   - Query `reasons` must not end in a dangling dash ("launched X 2024–"): open-ended ranges read "since 2024" or just "2024" for point-in-time predicates (API `reasons` formatting).
   - People whose only name is an HN handle: show "hdubugras" with a subtle "HN handle" hint rather than as if it were a real name.
5. **Read latency.** On a copy of the live graph (`createdb -T s4_live s6_perf` on the shared server), measure p50/p95 for search, entity view, edges, and the five example queries on the home page. Target p95 < 50 ms; the "everyone who founded a company" query currently takes ~80 ms. Fix with query/index changes (EXPLAIN as `rebox_app`), not by caching.
6. **Docs** — make them true for a newcomer: `README.md` quickstart (verified literally), `docs/RUNBOOK.md` updates, new `docs/ARCHITECTURE.md` (one page: concepts, services, data flow, layers/RLS, where to extend: new connector, new predicate, new endpoint), new `docs/STATUS.md` (what v0 does, numbers from the live run, known gaps, next steps: private layers/firm instance, activity store, more sources, AWS).
7. **CI parity.** Run each command from `.github/workflows/ci.yml` locally; all must pass.

## Verification
- Paste `docker compose ps`, the migrate log tail, worker logs showing hn + wikidata sync stats, `/v0/stats` from the containerized API, and `curl -s localhost:<port>/ | head` from the stack.
- Screenshots (Playwright chromium is installable) of Home, an entity with facts from both sources, Graph, and Query on the containerized stack → `.screens/` (gitignored) — list paths.
- Latency table before/after for item 5.
- All suites green: api, web, worker (pytest + ruff), terraform validate, compose config.
- Stop the stack and dockerd at the end (leave volumes if useful, but list them).
