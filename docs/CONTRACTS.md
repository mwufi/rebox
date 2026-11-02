# Rebox v0 — Contracts

These are the interfaces between services. Every slice builds against this file. If a slice needs to change a contract, it stops and reports back instead of changing it unilaterally.

## 1. Concepts (the whole data model)

| Concept | What it is | Table |
| --- | --- | --- |
| Entity | a thing: person, company, launch, event, place | `entity` |
| Claim | one fact about an entity, in one layer: subject + predicate + (object entity **or** value) | `claim` |
| Layer | who can see a set of claims, and where they came from | `layer` |
| Predicate | the vocabulary: which kinds of facts exist | `predicate` |

- A predicate with `object_kind` set is an **edge** type; claims using it must have `object_id`. With `object_kind` null it is an **attribute** type; claims must have `value` and no `object_id`.
- Attribute `value` is a JSON scalar (or array). Edge `value` is an object of edge props, or null.
- Every entity has a `name` claim (predicate `name`, value a JSON string). Search uses it.
- `cardinality = 'one'`: the fold keeps one winner per subject+predicate. `'many'`: keeps every distinct object/value.

Schema: `db/migrations/0001_core.sql` (+ `0004_rls_indexes.sql`, see §2a). Operational tables (change feed, jobs, connector state, raw index, resolution log, review queue, merge log): `db/migrations/0002_ops.sql`. Vocabulary: `db/seed/0001_vocabulary.sql`.

## 2. Layers and access

- Layer ids: `public/<source>` (e.g. `public/wikidata`), `firm/<source>`, `team/<team>/<source>`, `user/<user>/<source>`.
- Precedence for the fold (narrowest wins): `user` > `team` > `firm` > `public`; ties within a scope break by newest `updated_at`.
- **Database access always goes through role `rebox_app`** (RLS applies; superusers bypass it). Owner/superuser URLs are only for migrations and test setup.
- **Every transaction must first call** `select rebox_set_view(visible text[], writable text[])`. With nothing set, a session sees and writes nothing.
- v0 viewers:
  - anonymous: visible = all `public/*` layers; writable = none.
  - ingest token (worker): visible = all `public/*`; writable = the one layer named in the request.
  - admin token: visible = all `public/*`; writable = all `public/*` (merge/split only).
  - `REBOX_ENV=dev` only: header `X-Rebox-Viewer: <user id>` adds `user/<id>/*` and their teams' layers (for testing private layers).
- Compute "all public layers" as `select id from layer where scope = 'public'`.

### 2a. Querying under RLS (indexes)

Under RLS, Postgres uses an index only when the operator is leakproof. `value #>> '{}'` and pg_trgm `%` are not. Therefore:
- **Compare attribute values with `claim.value_text`** (a stored generated column = `value #>> '{}'`), never with `value #>> '{}'` or `value->>...`. Index: `(predicate, value_text) where object_id is null`. Use it for hard-key lookups and `attr eq` filters.
- **Name search uses `search_name`** (entity_id, layer_id, name; trigram-indexed; no RLS because it only holds names from `public/*` layers, maintained by trigger). Search = trigram match on `search_name` for public names, unioned with an RLS-filtered scan of `claim where predicate = 'name' and layer_id not like 'public/%'` for the viewer's private names. Exclude merged entities via `entity.merged_into is null`.

## 3. Entity ids (deterministic minting)

- Format: `rbx:<kind>:<id>` where `<id>` matches `[a-z0-9-]+`. Private, unshared entities use `local:<kind>:<id>` (not used in v0).
- Ids are minted by the **worker resolver** and are deterministic, so retries never create duplicates:
  - `id = base32_lower_nopad(sha256(f"{kind}|{key}|{normalized_value}"))[:20]`, using the entity's strongest identity key in this order: `wikidata`, `domain`, `github`, `x_handle`, `hn_user`, `hn_story`, `url`, `website`, `email`.
  - With no identity key: `sha256(f"{kind}|src|{source}|{source_ref}")`.
- Seeded fixed ids are allowed for well-known entities, e.g. `rbx:place:san-francisco`.

## 4. Normalizers (identity values are stored normalized)

| name | rule | example |
| --- | --- | --- |
| `qid` | uppercase, must match `Q\d+` | `q42` → `Q42` |
| `handle` | lowercase, strip leading `@`, strip URL prefix | `https://x.com/Jack` → `jack` |
| `domain` | lowercase host, strip scheme, `www.`, port, path | `https://www.Stripe.com/about` → `stripe.com` |
| `url` | lowercase scheme+host, drop `www.`, drop trailing `/`, drop query/fragment | `https://www.acme.ai/` → `https://acme.ai` |
| `email` | lowercase, trim | |
| `exact` | trim only | |

Name matching (resolver fuzzy pass): lowercase, strip punctuation and accents, collapse whitespace.

## 5. Claims writer API (the only write path)

`POST /internal/claims` — `Authorization: Bearer $INGEST_TOKEN`

```json
{
  "layer": "public/wikidata",
  "claims": [
    { "subject": {"mint": {"id": "rbx:company:abc", "kind": "company"}},
      "predicate": "name", "value": "Stripe", "source_ref": "wikidata:Q7624104" },
    { "subject": {"id": "rbx:person:def"}, "predicate": "worked_at",
      "object": {"id": "rbx:company:abc"}, "value": {"title": "CTO"},
      "valid_from": "2012-01-01", "source_ref": "wikidata:Q7624104:P112:Q123" }
  ]
}
```

- `subject` / `object` is `{"id": ...}` (must already exist and be visible) or `{"mint": {"id", "kind"}}` (created if missing, `home_layer` = request layer; if it exists already it is reused).
- If `layer` matches `public/<source>` and does not exist, the writer creates it (`scope='public', source=<source>`).
- Optional per claim: `valid_from`, `valid_to` (ISO dates/timestamps), `retract` (bool).
- Validation per claim: predicate exists; edge/attribute shape matches; subject kind ∈ `subject_kinds`; object kind = `object_kind`; predicates with `meta.private = true` are rejected in `public/*` layers; identity predicates have normalized values.
- Semantics: one transaction for the whole batch (max 1000 claims). Upsert on (layer, source_ref, subject, predicate, object): same key = same claim, latest write wins.
- Response `200`: `{"written": n, "minted": m}`. Response `400`: `{"error": "validation", "details": [{"index": i, "message": "..."}]}` and nothing is written.

## 6. Read API v0 (public, anonymous)

All responses are JSON. Entity references in responses are `{"id","kind","name"}` ("EntityRef").

| Method + path | Returns |
| --- | --- |
| `GET /health` | `{"ok": true, "db": true}` |
| `GET /v0/stats` | `{"entities": {kind: n}, "claims": n, "layers": [{id, claims}], "last_seq": n}` |
| `GET /v0/predicates` | `[{id, subject_kinds, object_kind, cardinality, meta}]` |
| `GET /v0/search?q=&kind=&limit=20` | `[{"id","kind","name","score"}]` (pg_trgm on `name`, merged entities excluded) |
| `GET /v0/entities/:id` | Entity view (below). If merged, `301`-style JSON: `{"redirect": "<into id>"}` with status 200 and the target's view under `"entity"` |
| `GET /v0/entities?ids=a,b` | `[EntityRef]` |
| `GET /v0/entities/:id/edges?predicate=&dir=out\|in&limit=50&cursor=` | `{"edges": [Fact], "next": cursor\|null}` |
| `POST /v0/query` | Query (below) |
| `GET /v0/changes?after=0&limit=500` | `{"changes": [{seq, op, layer_id, subject, predicate, object_id, at}], "next": seq}` |

Entity view:
```json
{ "id": "rbx:person:abc", "kind": "person", "name": "Maya Chen", "merged_from": ["rbx:person:old"],
  "facts":    [Fact],   // outgoing: this entity is the subject (after the fold)
  "incoming": [Fact] }  // edges where this entity is the object
```
Fact:
```json
{ "predicate": "worked_at", "value": {"title": "CTO"}, "object": EntityRef | null,
  "subject": EntityRef, "valid_from": "...", "valid_to": null,
  "layer": "public/wikidata", "source": "wikidata", "source_ref": "...", "updated_at": "..." }
```

Query (AND of filters; v0):
```json
{ "find": "person",
  "where": [
    { "name": "chen" },
    { "edge": { "predicate": "worked_at", "dir": "out", "to": "rbx:company:abc",
                "during": { "from": "2018-01-01", "to": "2021-12-31" } } },
    { "edge": { "predicate": "founded", "dir": "out" } },
    { "attr": { "predicate": "yc_batch", "eq": "W24" } }
  ],
  "limit": 50, "cursor": null }
```
→ `{"results": [{ "id", "kind", "name", "reasons": ["worked_at Stripe 2019–2021"] }], "next": cursor|null}`
- `edge.to` optional (any object); `dir: "in"` matches edges pointing at the result entity; `during` = validity overlaps the range.
- `attr` supports `eq`, `gte`, `lte`, `contains` (text, case-insensitive).

## 7. Admin API v0 — `Authorization: Bearer $ADMIN_TOKEN`

| Method + path | Effect |
| --- | --- |
| `GET /v0/review?status=open` | `[review_item + candidate EntityRefs]` |
| `POST /v0/admin/merge {"from","into","review_id?"}` | sets `entity.merged_into`, logs `merge_log`, resolves review item, emits `change` op `merge` |
| `POST /v0/admin/split {"id"}` | clears `merged_into`, logs, emits `split` |
| `POST /v0/admin/review/:id/dismiss` | marks dismissed |

Reads follow `merged_into` chains: facts of merged-away entities appear on the target; search and query return only un-merged entities.

## 8. Worker contracts

**RawRecord** (JSON file in the raw store):
```json
{ "source": "hn", "external_id": "story:41234567", "url": "https://...",
  "fetched_at": "2026-11-01T00:00:00Z", "content_hash": "sha256:...", "payload": { ... } }
```
- Store key: `{source}/{yyyy}/{mm}/{dd}/{sha256(external_id)[:16]}.json`. Indexed in table `raw_record`; unchanged `content_hash` is skipped on re-fetch.
- `RAW_STORE` = `file:///abs/path` or `s3://bucket/prefix`.

**Connector** (Python, `apps/worker/rebox_worker/connectors/<name>.py`):
```python
class Connector(Protocol):
    name: str                      # 'hn'
    layer: str                     # 'public/hn'
    interval_s: int                # schedule
    def fetch(self, ctx: Ctx, cursor: dict | None) -> Iterator[RawRecord | CursorUpdate]: ...
    def extract(self, rec: RawRecord) -> list[ClaimInput]: ...   # pure; no network, no DB
```

**ClaimInput / EntityRef** (before resolution):
```python
EntityRef = {"id": str} | {"kind": str, "identity": {predicate: raw_value}, "name": str | None,
                           "context": [EntityRef] | None}   # context = related entities for scoped fuzzy match
ClaimInput = {"subject": EntityRef, "predicate": str, "object": EntityRef | None,
              "value": Any, "valid_from": str | None, "valid_to": str | None, "source_ref": str}
```
Extractors emit identity facts as ordinary claims too (e.g. a `wikidata` claim), so identities are queryable.

**Resolver** (worker, reads DB as `rebox_app` with all public layers visible): for each unresolved EntityRef, in order:
1. **hard keys**: normalized identity value matches an existing identity claim → that entity (following `merged_into`). Several distinct entities → open a `review_item` (`ambiguous_match`) and use the oldest.
2. **scoped fuzzy**: if `context` entities resolved, a same-kind entity with equal normalized name that has any edge to a context entity → match (confidence 0.8).
3. **mint**: deterministic id (section 3).
Every decision writes `resolution_log`. Batch-local cache so one batch resolves consistently.

**Jobs**: table `job`, claimed with `FOR UPDATE SKIP LOCKED`; kinds `sync` (`{"source"}`) and `replay` (`{"source"}`: re-extract from raw store, no fetch). `connector_state` holds cursor and schedule.

## 9. Environment variables

| Var | Used by | Example |
| --- | --- | --- |
| `DATABASE_URL` | api, worker | `postgres://rebox_app:rebox_app@localhost:54329/rebox` (app role) |
| `MIGRATE_DATABASE_URL` | migrate | owner URL (password may come from `PGPASSWORD`) |
| `APP_DB_PASSWORD` | migrate | password set on role `rebox_app` |
| `PORT` | api | `3000` |
| `INGEST_TOKEN` | api, worker | secret |
| `ADMIN_TOKEN` | api | secret |
| `API_URL` | worker | `http://localhost:3000` |
| `RAW_STORE` | worker | `file:///abs/.data/raw` or `s3://bucket/raw` |
| `REBOX_ENV` | api, worker | `dev` \| `prod` |
| `USER_AGENT` | worker | `rebox-bot/0.1 (+https://rebox.example)` |
| `GITHUB_TOKEN` | worker (optional) | enables live GitHub connector |
| `X_BEARER_TOKEN` | worker (optional) | enables live X connector |
| `AWS_REGION` | worker (s3 raw store) | `us-west-2` |
| `SCHEDULE_INTERVAL_S` | compose scheduler | `900` |

## 10. Repository conventions

- Monorepo: `apps/api` (TypeScript, Node 22, Hono, `postgres` driver, vitest), `apps/web` (React + Vite, built into `apps/api/public`), `apps/worker` (Python 3.13, uv, psycopg 3, httpx, pytest), `db/`, `infra/`, `fixtures/raw/`, `docs/`.
- Tests use a real Postgres: `scripts/test-db.sh <dbname>` prints the owner URL then the app URL. Shared dev server: `postgres://postgres@localhost:54329/postgres`. Each slice uses its own database names prefixed with its slice id.
- No service imports another service's code. They share only these contracts and the database schema.

## 11. Packaging (from S3)

- `apps/api`: `pnpm --filter @rebox/api build` must emit `dist/server.js`; the image runs `node dist/server.js` from `/app/apps/api` and serves static files from `apps/api/public` (resolve it relative to `dist/..`).
- `apps/worker`: console script `rebox-worker` must support `--help`; the image runs from `/app` with fixtures at `/app/fixtures/raw/<source>` (resolve fixtures relative to the repo root, overridable by `FIXTURES_DIR`).
- Commit `pnpm-lock.yaml` and `apps/worker/uv.lock`; images use frozen/locked installs when present.
