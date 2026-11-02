# S8 — First-party spiders: X following graph + Exa discovery

**Goal:** Rebox's own crawlers discover the SF startup world: who the best-connected SF people follow on X (people and companies), ranked by how many seed accounts follow them, plus fresh companies and launches found through Exa. They write clean, attributable claims and a frontier of what to index next.

**Branch:** `s8-spiders`  **DB prefix:** `s8_`  **Contract:** CONTRACTS §12, §13, predicates `x_id`, `avatar`, `follows`, `milestone`.

## Owns
`apps/worker/rebox_worker/connectors/{x_spider,exa}.py`, `apps/worker/rebox_worker/frontier.py`, `apps/worker/rebox_worker/xapi.py` (shared X API client, may refactor `connectors/x.py` to use it), `config/seeds.yaml`, `apps/worker/tests/spiders/**`, `fixtures/raw/{x_spider,exa}/**`, `docs/SOURCES.md` (append), registry lines.

## X spider (`x_spider`, layer `public/x_spider`)
- **Seeds:** `config/seeds.yaml` lists named seed sets, each becoming its own worker identity, e.g. `dbooth: [davidbooth]`, `yoni: [yoni_michael]`, and an `sf-core` set the architect will extend. Seeds are X usernames.
- **Discovery:** for each seed, page `GET /2/users/:id/following` (`max_results=1000`, `user.fields=description,location,url,entities,public_metrics,profile_image_url,verified_type,created_at`). Each followed account is (a) upserted into `frontier` (`source='x_spider'`, `key=<x user id>`, `score = number of distinct seeds following it`, `discovered_by += seed`) and (b) emitted as a RawRecord `x_user` (the user object as returned — no extra profile call needed), plus a `follows` edge seed → account.
- **Expansion:** after the seeds, take the top frontier accounts by score that look like SF people (location/bio mentions SF, Bay Area, or no location but followed by ≥ 2 seeds) and treat them as depth-1 seeds, until the per-run budget `X_MAX_USER_READS_PER_RUN` is spent. Store the cursor so the next run continues.
- **Posts:** for accounts classified as companies and for the top-scored people, fetch up to 20 recent original posts (`GET /2/users/:id/tweets`, exclude replies/retweets) within `X_MAX_POST_READS_PER_RUN`; save each as a RawRecord `x_post` (consumed by the parser slice; emit no claims from posts here).
- **Extract (deterministic) for `x_user`:** `x_id` (identity), `x_handle`, `name`, `headline` (people) or `description` (companies) from the bio, `location` (people), `website` from `entities.url` (expanded, never t.co), `avatar` (profile_image_url, larger size), `x_followers`. Kind: `company` when `verified_type = 'business'` or the bio/name match clear company signals (the account's website domain equals its name, "official account", etc.); otherwise `person`. Also emit `domain` for companies from their website. Mentions like `@acme` in a bio are left for the parser.
- **Identity:** resolve by `x_id` first, then `x_handle` (so accounts already in the graph from Wikidata/GitHub/X enrichment are matched, not duplicated).
- **Cost safety:** never exceed the budgets; log `api_calls` and an estimated `cost_usd` (assume $0.01 per user object and $0.005 per post unless the response headers say otherwise) into run stats.

## Exa discovery (`exa`, layer `public/exa`)
- Config `config/seeds.yaml` → `exa.queries`: a list of natural-language queries about the SF startup world (e.g. "San Francisco startup launched in the last 30 days", "seed round announced San Francisco this month", "YC company launch this week", and named hot startups the architect lists). Use Exa search with `numResults` ≤ 25, recent date filters, and contents (text summary/highlights) within `EXA_MAX_REQUESTS_PER_RUN`.
- Each result → RawRecord `exa_result` (url, title, published date, text/highlights). Deterministic extract only for company homepages: `company` with `domain`, `name` (cleaned site title), `website`. Articles (news, launch posts, funding announcements) produce no claims here; the parser slice extracts them.
- Frontier rows for discovered domains (`source='exa'`, `key=domain`).

## Verification
- Offline tests (network blocked): x_user extraction table (person vs company, website expansion, avatar sizing, location), follows edges, frontier scoring/expansion order with budgets, cursor resume, Exa extraction; fixtures recorded live then trimmed (keep ≤ 30 users, ≤ 20 results; X users are public accounts — keep only fields we use).
- Live smoke against a fresh `s8_live` DB with the real API from your worktree (merge `main` first): `sync x_spider` with seeds `dbooth` and `yoni` and `X_MAX_USER_READS_PER_RUN=400`, `X_MAX_POST_READS_PER_RUN=60`; `sync exa` with `EXA_MAX_REQUESTS_PER_RUN=10`. Report entity counts by kind, top 20 frontier accounts by score with labels, company vs person split, matches against existing entities, run stats incl. cost, and 3 sample entity views.
- Secrets: source `/home/claude/rebox/secrets/keys.env` for live runs only. Never print, log, commit or write the key values anywhere.
