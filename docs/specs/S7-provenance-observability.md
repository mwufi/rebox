# S7 — Worker identity, provenance and observability

**Goal:** every claim is attributable to a named worker and run; anyone can see how each worker is doing (throughput, errors, freshness) in the explorer and in a Grafana dashboard of the graph growing over time.

**Branch:** `s7-provenance`  **DB prefix:** `s7_`  **Contract:** CONTRACTS §12, §14 (workers, timeseries rows), migration `0005_provenance.sql`.

## Owns
- `apps/api`: writer changes, new `src/routes/workers.ts` (+ internal worker/run routes), fold tie-break, Fact view fields (`worker`, `evidence`, `support`), `/v0/stats/timeseries`, admin worker-token issuance, tests.
- `apps/worker`: `client.py` (sends `worker`, `run_id`, `evidence`), worker identity module (register/resolve per connector, persisted ids), run tracking in `pipeline.py`, CLI `rebox-worker workers list|register|show`, tests.
- `apps/web`: a **Workers** page (`/workers`, `/workers/:id`) and its nav link; show a fact's worker, evidence links (each as "Source ↗" with author) and support count ("2 sources") on the entity page's fact rows.
- `infra/grafana/**`, `docker-compose.yml` (add a `grafana` service on host port 3001 with provisioned datasource + dashboard; add every new env var from CONTRACTS §9 to the worker/api env blocks; migrate sets the `rebox_metrics` password from `METRICS_DB_PASSWORD`), `.env.example` (new vars, empty secrets), `infra/docker/migrate.sh`.

## Build
1. **Writer + identity (api):** implement §12 exactly: `POST /internal/workers`, `POST/PATCH /internal/runs`, worker/run/evidence on `POST /internal/claims` with the validation rules, external worker tokens (`POST /v0/admin/workers`, hashed, constant-time check), `last_seen_at`. Update existing tests that post claims to include a worker.
2. **Worker side:** each connector resolves a stable worker identity on first use (`<connector>-connector-<6 digits>` by default; spiders/parsers choose their own name, e.g. a spider seeded from davidbooth is `dbooth-spider-xxxxxx`) and persists it in `connector_state` (add nothing to the schema: store it in `cursor` under a reserved key, or in `worker.config` lookups by `(name, kind, source)` — choose the cleanest and document it). `run_sync`/`run_replay` open a run, send `run_id` with every batch, PATCH final stats; failures close the run as `failed` with the error.
3. **Fold + views:** trust tie-break; `worker`, `evidence`, `support` on Fact (support must stay index-friendly; measure on a copy of `s4_live`).
4. **Stats:** `/v0/workers`, `/v0/workers/:id`, `/v0/stats/timeseries` per §14, computed from `change`, `run`, `worker` (no full scans of `claim`; add indexes in a new migration `db/migrations/0006_*.sql` only if needed and say why).
5. **Explorer:** Workers list (kind, trust, status, last seen, claims 24 h, a small claims-per-hour sparkline, last run status) and worker detail (runs table with stats and durations, throughput chart, predicates histogram, recent facts linking to entities). Match the existing design system in `apps/web/src/styles.css`.
6. **Grafana:** compose service `grafana` (official image; Docker Hub may rate-limit, `public.ecr.aws`/mirror tags are fine), anonymous read-only viewer enabled, provisioned Postgres datasource as `rebox_metrics`, one dashboard "Rebox graph" with: entities over time by kind (cumulative, from first claim per subject in `change`; kind from the id prefix), claims per hour by worker, runs by status, run duration, review queue size, frontier size by status, connector last-run age. Commit dashboard JSON under `infra/grafana/`.

## Verification
- api: typecheck, build, all tests (existing + new: worker registration idempotency, layer permission, evidence-required kinds, external token auth incl. revoked, trust tie-break in the fold, support counts, timeseries buckets).
- worker: pytest + ruff; an end-to-end test where two workers write the same fact (support = 2) and runs close with stats.
- web: typecheck, test, build.
- Live: start api from your worktree on a copy (`createdb -T s4_live s7_live`, then migrate it to 0005+ with `db/migrate.sh` as owner), run `rebox-worker sync hn --limit 100` and `sync example`; paste `/v0/workers`, `/v0/workers/:id` and a timeseries response.
- Grafana: if `dockerd` starts, run the grafana service against your `s7_live` DB (host network or compose) and capture a screenshot of the dashboard with Playwright into `.screens/`; if not, validate the provisioning YAML/JSON and the SQL of every panel by running it as `rebox_metrics`.
