# S4 — Public connectors: Wikidata, Hacker News launches, GitHub, X

**Goal:** three real connectors that build the first public graph of the SF startup world, plugged into the worker's Connector protocol, tested offline against recorded fixtures, and smoke-tested live end to end through the real API.

**Branch:** `s4-connectors`  **DB prefix:** `s4_`  **Depends on:** S1 (api) and S2 (worker core) merged to `main`.

## Owns
`apps/worker/rebox_worker/connectors/{wikidata,hn,github,x}.py`, `apps/worker/tests/connectors/**`, `fixtures/raw/{wikidata,hn,github,x}/**`, `docs/SOURCES.md`. One line in the connector registry per connector.

## Rules for every connector
- `fetch` is the only code that touches the network. Respect rate limits (sleep between requests, exponential backoff on 429/5xx), send `USER_AGENT`, support `limit` (stop after N raw records) for dev runs.
- `extract` is pure: RawRecord → ClaimInputs. `source_ref` is stable and specific: `<source>:<external id>:<predicate>[:<object key>]`.
- Emit identity facts as claims (e.g. `wikidata`, `domain`, `hn_user`) and a `name` for every entity.
- Use EntityRef `context` when a person is only meaningful next to a company (e.g. a founder), so the scoped fuzzy pass can match them.
- Never emit predicates with `meta.private = true`.

## Wikidata (`public/wikidata`, CC0)
- SPARQL against `https://query.wikidata.org/sparql`. Scope: organizations headquartered (P159) in San Francisco (Q62); optionally a configurable list of Bay Area cities. Note: transitive `P31/P279*` filters time out; prefer simple patterns and filter in Python.
- Per organization: label, description, official website (P856) → `website` + `domain`, X username (P2002), GitHub username (P2037), inception (P571) → `founded_year`, employee count (P1128) → `headcount`, `based_in` → San Francisco place (identity `wikidata: Q62`).
- People: founded by (P112) → `founded`, CEO (P169) → `ceo_of`; for each person: label, description, website, X, GitHub, Wikidata id.
- One RawRecord per organization (all of its bindings grouped), `external_id = <QID>`. Cursor: none (full refresh weekly, `interval_s = 604800`); unchanged records are skipped by content hash.

## Hacker News launches (`public/hn`)
- Algolia API `https://hn.algolia.com/api/v1/search_by_date?query="Launch HN"&tags=story`, paginated; cursor = max `created_at_i` seen; first run backfills everything (~1–2k stories). `interval_s = 3600`.
- Parse titles like `Launch HN: Acme (YC W24) – Tagline`: company `name`, `yc_batch`; company identity `domain` from the story URL when it is not a news.ycombinator.com URL.
- Launch entity: identity `hn_story` = objectID; `name` = tagline, `url`, `points`; `valid_from` = created_at.
- Edges: company `launched` launch; author person (identity `hn_user`, context = [company]) `posted` launch, and `founded` company with `value: {"basis": "launch_hn_author"}` (YC requires Launch HN posts to come from founders).
- Titles that don't parse are kept as raw records but produce no claims; count them in stats.

## GitHub (`public/github`)
- Live mode requires `GITHUB_TOKEN`; without it the connector reports itself disabled (`enabled()` false) and `schedule_due` skips it. Tests run on fixtures.
- Search users with `location:"San Francisco"` and `followers:>=100` (split by follower ranges to stay under the 1,000-result search cap), then fetch each profile; for `company` values of the form `@org`, fetch the org.
- Person: `name`, `github` (login), `x_handle`, `website`, `headline` (bio), `location`. Company (only from `@org`): `name`, `github`, `website` + `domain`, `x_handle`. Edge: person `worked_at` company (no dates).
- **Fixtures must be synthetic** (clearly fake logins like `example-octo-1`), because no GitHub access exists in this environment and we don't want copies of real people's profiles in git.

## X profiles (`public/x`)
- Live mode requires `X_BEARER_TOKEN`; without it `enabled()` is false. Tests run on synthetic fixtures (fake handles only).
- **Enrichment, not discovery:** read the `x_handle` values already in the graph (people and companies, from Wikidata, GitHub and others) through the worker's DB access, then look them up in batches of up to 100 with the X API v2 users-by-username endpoint (`user.fields=name,description,location,url,public_metrics`). Respect rate-limit headers; cursor = last handle processed so a run can resume.
- Emits for the existing entity (EntityRef with identity `x_handle`): `name` only if the entity has none, `headline` (description, people) or `description` (companies), `location` (people), `website` (expanded URL, not t.co), `x_followers`.
- Posts and engagement (for "who posted about AI") are out of scope for v0; they need an activity store and a paid API tier.

## docs/SOURCES.md
For each source: what is fetched, license/terms, rate limits used, schedule, layer, and the predicates it emits.

## Verification
- `uv run pytest -q tests/connectors` passes offline (network disabled in tests): extraction from fixtures produces the expected entities and claims; title parsing table for HN (≥ 15 real title shapes, including ones without a batch or URL); Wikidata grouping; GitHub `@org` handling; no private predicates.
- Record fixtures: run each live fetch (Wikidata, HN) with a small limit and save ~20–40 raw records per source into `fixtures/raw/<source>/`.
- **Live end-to-end smoke** against a fresh `s4_live` database with the real API running from your worktree: `rebox-worker sync hn --limit 300` and `rebox-worker sync wikidata --limit 200`, then report: entity counts by kind, claims per layer, how many companies were matched across the two sources by domain (dedupe evidence), review items opened, and 3 sample `/v0/entities/:id` responses (one company with facts from both layers).
- `rebox-worker replay hn` on the same DB creates zero new entities.

## Also in this slice (follow-ups from S2 review)
- `apps/worker/rebox_worker/resolve.py` and its tests: switch value comparisons from `value #>> '{}'` to `claim.value_text` (CONTRACTS §2a, migration 0004) so hard-key and fuzzy lookups are true index lookups under RLS; keep the lateral-per-predicate shape only if it still helps. Add a test asserting the hard-key query plan, as `rebox_app` on ≥ 10k analyzed rows, uses `claim_value_text_idx`.
- If the S1 API is not on `main` yet when you reach the live end-to-end smoke, run it through the worker's fake writer started as a standalone server (as S2 did), state that in the report, and the architect will rerun it against the real API.
