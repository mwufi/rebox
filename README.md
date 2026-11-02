# Rebox

An open graph of people, companies, launches and events, starting with the San Francisco startup world.

## How it fits together

```
connectors ─► raw store ─► extract ─► resolve ─► POST /internal/claims ─► Postgres ─► read API + explorer
 (worker)      (bucket)    (worker)   (worker)          (api)            (claims,     (api + web)
                                                                          change feed)
```

- **Four concepts:** entity, claim, layer, predicate. See `docs/CONTRACTS.md`.
- **Two services:** `api` (TypeScript: read API, claims writer, explorer UI) and `worker` (Python: connectors, resolver, jobs), plus Postgres and a bucket.
- **Every fact is a claim in a layer.** Public layers are `public/<source>`; private layers come later with Rebox for Firms.

## Layout

| Path | What |
| --- | --- |
| `apps/api` | Hono API: `/v0/*` reads, `/internal/claims` writes, admin merge/split; serves the explorer |
| `apps/web` | Explorer UI (React + Vite), built into `apps/api/public` |
| `apps/worker` | Connectors, raw store, resolver, job queue, CLI `rebox-worker` |
| `db/` | SQL migrations, seed vocabulary, `migrate.sh` |
| `infra/` | Dockerfiles, Terraform for AWS, deploy scripts |
| `fixtures/raw/` | Recorded raw records for offline runs and tests |
| `docs/` | Contracts, slice specs, runbooks |

## Local development

Requires Docker (or a local Postgres 16), Node 22 + pnpm, Python 3.13 + uv.

```bash
cp .env.example .env
make up          # Postgres in Docker on :54329
make migrate     # schema + vocabulary + app role
make api         # http://localhost:3000 (explorer at /)
make worker      # processes queued jobs
make sync SOURCE=hn     # run one connector now
make test        # all test suites
```

See `docs/RUNBOOK.md` for deploying to AWS.
