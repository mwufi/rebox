# Rebox

An open graph of people, companies, launches and events, starting with the San Francisco startup world.

## How it fits together

```
 spiders / connectors ─► raw store ─► extract ─► resolve ─► POST /internal/claims ─► Postgres ─► read API + explorer
 (X following, Exa,       (files/S3,   (pure)     (hard keys,        (api: the one         (claims,      (api + web,
  Wikidata, HN, GitHub)    replayable)            fuzzy, name,        write path)           change feed)  Grafana)
          │                    ▲                   mint)
          ▼                    │
 frontier (who to fetch next)  └── parsers (Haiku reads bios/posts/articles; model output is stored raw,
                                    so replay never calls the model) ─► evidence-backed claims

 maintenance: dedupe (merge shared keys, review lookalikes) · wiki generation · coverage checks · export/import
```

- **Four concepts:** entity, claim, layer, predicate. See `docs/CONTRACTS.md`.
- **Every fact is a claim in a layer, written by a worker in a run.** Public layers are `public/<source>`; private layers come later with Rebox for Firms. Workers (`dbooth-spider-465908`, `xbio-parser-229837`, …) have trust; the fold picks a winner by scope, then trust, then recency.
- **Two services:** `api` (TypeScript: read API, claims writer, explorer, wikis) and `worker` (Python: connectors, parsers, resolver, jobs, maintenance commands), plus Postgres, a raw store and Grafana.

## Layout

| Path | What |
| --- | --- |
| `apps/api` | Hono API: `/v0/*` reads, timelines, wikis, `/internal/*` writes and runs, admin merge/split/workers; serves the explorer |
| `apps/web` | Explorer UI (React + Vite): search, entity pages with timeline + wiki + provenance, graph, query, review, workers |
| `apps/worker` | Connectors, spiders, parsers, raw store, resolver, job queue, CLI `rebox-worker` |
| `config/` | `seeds.yaml` (who the spiders start from, Exa queries), `trust.yaml` (trust per source) |
| `contracts/` | Cases both services must agree on (normalizers) |
| `db/` | SQL migrations, seed vocabulary, `migrate.sh` |
| `infra/` | Dockerfiles, Grafana provisioning, Terraform for AWS |
| `fixtures/` | Recorded raw records and sample lists for offline runs and tests |
| `docs/` | Contracts, runbooks, slice specs |

## Local development

Requires Docker (or a local Postgres 16), Node 22 + pnpm, Python 3.13 + uv.

```bash
cp .env.example .env   # add API keys to turn on live connectors/parsers (all optional)
make up          # Postgres in Docker on :54329
make migrate     # schema + vocabulary + app role
make api         # http://localhost:3000 (explorer at /)
make worker      # processes queued jobs
make sync SOURCE=hn     # run one connector now
make test        # all test suites
```

Or the whole stack: `docker compose up --build` (API + explorer on :3000, Grafana on :3001).

## Worker commands you'll use

```bash
rebox-worker sync x_spider          # crawl X following lists from config/seeds.yaml (budget-guarded)
rebox-worker sync x_parse           # Haiku reads new bios -> roles, companies, locations (with quotes)
rebox-worker replay <source>        # re-extract from the raw store (no network, no model calls)
rebox-worker dedupe --dry-run       # merge entities that share a key; review lookalikes
rebox-worker coverage list.csv      # how much of a list Rebox knows (docs/COVERAGE.md)
rebox-worker export / import        # move layers between instances (docs/MERGE.md)
rebox-worker raw reindex <source>   # repair the raw index after a crashed run
rebox-worker workers list           # who wrote what
```

## Docs

| Doc | What |
| --- | --- |
| `docs/CONTRACTS.md` | The data model, ids, normalizers, APIs, workers, provenance — the source of truth |
| `docs/STATUS.md` | What's built, what's live, known gaps, what's next |
| `docs/SOURCES.md` | Each source: what it fetches, cost, terms |
| `docs/PARSING.md` | The Haiku parsers: prompts, guards, eval numbers |
| `docs/DEDUPE.md` | What gets merged automatically vs reviewed, and how to undo |
| `docs/MERGE.md` | Export a graph and import it into another instance |
| `docs/COVERAGE.md` | Check a list against the graph |
| `docs/RUNBOOK.md` | Operating the stack (and AWS, when we get there) |
| `docs/specs/` | The slice specs the system was built from |
