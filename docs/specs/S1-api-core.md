# S1 — API core (TypeScript)

**Goal:** a running `apps/api` service that implements the claims writer (CONTRACTS §5) and the read API (§6), enforcing layers through Postgres RLS, with tests against a real database.

**Branch:** `s1-api-core`  **DB prefix:** `s1_`

## Owns
`apps/api/**` (except `apps/api/public/`, which is a build output of the web app).

## Stack
Node 22, TypeScript (strict), Hono + `@hono/node-server`, `postgres` (porsager) driver, `zod` for request validation, vitest. Package name `@rebox/api`. Scripts: `dev` (tsx watch), `build` (tsc to `dist/`), `start` (`node dist/server.js`), `test` (vitest run), `typecheck`.

## Build

1. **DB access layer** (`src/db.ts`): a helper `withView(view, fn)` that opens a transaction, calls `select rebox_set_view($visible, $writable)`, then runs `fn(tx)`. All queries go through it. Resolve "all public layers" from the `layer` table (cache 30s).
2. **Auth** (`src/auth.ts`): anonymous; `Bearer $INGEST_TOKEN`; `Bearer $ADMIN_TOKEN`; and in `REBOX_ENV=dev` only, `X-Rebox-Viewer: <user>` adding `user/<user>/*` and `team/<t>/*` layers for teams in `team_member`. Constant-time token compare.
3. **Claims writer** `POST /internal/claims` exactly as §5: zod schema, batch ≤ 1000, per-claim validation against the `predicate` table (cache), mint entities, auto-create `public/<source>` layers, upsert claims with the unique key in `0001_core.sql`, one transaction, `{written, minted}` or `400` with per-index details.
4. **Fold** (`src/fold.ts`): effective facts for one or more subjects. Key = subject + predicate (+ object_id for edges, + value for `many` attributes). Winner = narrowest scope (`user` > `team` > `firm` > `public`), then newest `updated_at`. Drop winners with `retract = true`. Include facts of every entity whose `merged_into` chain points at the requested id.
5. **Read endpoints** in §6: `/health`, `/v0/stats`, `/v0/predicates`, `/v0/search`, `/v0/entities/:id`, `/v0/entities?ids=`, `/v0/entities/:id/edges`, `/v0/query`, `/v0/changes`. Names come from the folded `name` fact. Cursor pagination: opaque base64 of the last sort key.
6. **Query compiler** (`src/query.ts`): compile the §6 filter list to one SQL query (AND of `exists` subqueries over `claim`), plus `reasons` strings for the result rows. Reject unknown predicates and non-filterable attributes with `400`.
7. **Static explorer:** serve files from `apps/api/public` at `/` with SPA fallback to `index.html` for non-API paths, if the directory exists. Do not build the web app.
8. **Ops:** JSON request logs (method, path, status, ms), `X-Request-Id`, graceful shutdown, `PORT` default 3000, CORS `*` for `GET /v0/*`.

## Verification
Create `scripts` or test setup that runs `scripts/test-db.sh s1_test` and uses the **app URL** for the service and the owner URL only for fixture setup.

- `pnpm --filter @rebox/api typecheck` passes.
- `pnpm --filter @rebox/api test` passes, covering at least:
  - writer: mint + upsert idempotency (same batch twice → same row count), validation errors (edge without object, wrong subject kind, unknown predicate, private predicate into `public/*`, bad identity normalization) return 400 and write nothing.
  - RLS: a claim written to `user/alice/gmail` (via owner URL setup) is invisible to anonymous reads and to `X-Rebox-Viewer: bob`, and visible to `X-Rebox-Viewer: alice` in dev.
  - fold: a `user/*` `name` overrides the public `name` for that viewer only; a retract claim hides a public fact for that viewer only; `one` vs `many` behavior.
  - merge follow: facts from a merged-away entity appear on the target; search excludes merged entities.
  - search ranks "stripe" above "stripe press" for query `stripe`.
  - query: `worked_at` with `during` overlap, `attr eq`, `name`, combined AND, and `reasons` present.
  - changes feed returns inserts in seq order.
- Manual smoke: start the server against a seeded DB, `curl` `/health`, POST a 3-claim batch, then `curl /v0/search?q=` and `/v0/entities/:id`; paste outputs.
