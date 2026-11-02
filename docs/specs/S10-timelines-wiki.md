# S10 — Timelines, wikis, and explorer polish

**Goal:** every person and company has a history (a timeline of dated facts and milestones) and a one-click generated wiki article grounded in the graph's facts; plus the polish and performance items left over from S6.

**Branch:** `s10-timelines`  **DB prefix:** `s10_`  **Contract:** CONTRACTS §14 (timeline, wiki), §12 (Fact `evidence`, `support`, `worker` — rendered here only where S7 doesn't), migration 0005 (`entity_wiki`).

## Owns
`apps/api`: new `src/routes/{timeline,wiki}.ts`, `src/wiki/**` (Anthropic client, prompt), `query.ts` reasons formatting + performance, tests; a batch command `pnpm --filter @rebox/api wiki:batch -- --top N` (generates/refreshes wikis for the N most-connected entities). `apps/web`: entity page timeline + wiki sections/tab, avatar display (entity header, search results, graph nodes), date formatting fixes; tests. `@anthropic-ai/sdk` dependency + lockfile.

## Build
1. **Timeline API** (§14): events from dated facts on the entity (edges with `valid_from`/`valid_to` → "joined"/"left"; `milestone`; `launched` with the launch date; `founded` with founding year; `acquired`) and dated incoming edges (people joining a company, launches by its founders), plus "first seen in Rebox" from the change feed. Respect the fold and RLS (use the existing view helpers). Sort newest first, paginate.
2. **Timeline UI:** a vertical timeline on the entity page (year groupings, event icon per kind, linked entities, evidence "Source ↗" when the fact has evidence, a source badge). Launches/funding read as headline events.
3. **Wiki:** `GET/POST /v0/entities/:id/wiki` per §14 using `WIKI_MODEL` (default `claude-haiku-5-5`). Input = the entity's folded facts + timeline + 1-hop neighbors' names/roles + evidence URLs; the article must only state what the facts support, cite sources inline as numbered footnotes linking to evidence/source URLs, and say "Rebox doesn't know yet" rather than guess. Cache in `entity_wiki` with `input_seq`; `stale` when newer changes touch the entity. Rate-limit generation (e.g. 6/min). UI: a **Wiki** section with "Generate"/"Regenerate" (admin token from the Review page's sessionStorage unless `WIKI_PUBLIC`), rendered markdown with footnotes.
4. **Polish (from S6):** point-in-time predicates (`launched`, `posted`, `attended`, `milestone`, `acquired`) show one date ("Jun 2018"), never "– now"; query `reasons` never end in a dangling dash ("since 2024" or the single date); entities whose only name is a handle (HN/X) show the handle with a subtle "handle" hint.
5. **Performance (from S6):** on a copy of `s4_live`, measure p50/p95 for search, entity view, edges, timeline and the five home-page example queries; get every p95 under 50 ms (the "everyone who founded a company" query was ~80 ms). Fix with query/index changes verified by EXPLAIN as `rebox_app`; any index goes in a new migration `db/migrations/0007_*.sql` (explain why in the report).

## Verification
- api: typecheck, build, tests (timeline ordering, point-in-time vs ranges, RLS: private dated facts never leak, wiki prompt input contains only visible public facts, caching + staleness, auth/rate limit with a mocked model).
- web: typecheck, tests, build.
- Live: on `s10_live` (copy of `s4_live`, migrated to latest): latency table before/after; timeline for Brex and for a person; generate 3 wikis with the real model (Brex, Patrick Collison, one HN launch company) and paste them; Playwright screenshots of an entity page with timeline + wiki (`.screens/`).
- Secrets: source `/home/claude/rebox/secrets/keys.env` only for the live wiki generation; never print/log/commit key values.
