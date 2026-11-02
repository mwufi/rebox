# Sources

What each public connector fetches, under which terms, how politely, how often, into which layer, and which predicates it writes. Code: `apps/worker/rebox_worker/connectors/<name>.py`. Fixtures: `fixtures/raw/<name>/`.

Every connector sends `USER_AGENT` (default `rebox-bot/0.1`; set it to `rebox-bot/0.1 (+https://github.com/rebox)` or similar, and never to a personal email). Each one waits between requests and backs off exponentially on 429/5xx, honouring `Retry-After` / rate-limit reset headers. It supports `--limit N` for dev runs. `extract` is pure and stable. Identity facts (`wikidata`, `domain`, `github`, `x_handle`, `hn_user`, `hn_story`) are written as ordinary claims. No connector writes a `meta.private` predicate.

| Source | Layer | Schedule (`interval_s`) | Needs |
| --- | --- | --- | --- |
| Wikidata | `public/wikidata` | weekly (604800) | nothing |
| Hacker News launches | `public/hn` | hourly (3600) | nothing |
| GitHub | `public/github` | weekly (604800) | `GITHUB_TOKEN` (disabled without it) |
| X profiles | `public/x` | weekly (604800) | `X_BEARER_TOKEN` (disabled without it) |
| X following-graph spider | `public/x_spider` | on demand only (`scheduled = False`, paid API) | `X_BEARER_TOKEN`, `config/seeds.yaml` |
| Exa discovery | `public/exa` | daily (86400) | `EXA_API_KEY` (disabled without it), `config/seeds.yaml` |

---

## Wikidata — `wikidata`

**Fetched.** SPARQL from `https://query.wikidata.org/sparql`:
1. Discovery: every item with headquarters location (P159) in San Francisco (Q62). `WIKIDATA_CITIES=Q62,Q49255,...` adds more cities. The query also returns classes (P31), websites (P856), inception (P571), and a second simple query returns X/GitHub usernames. Transitive `P31/P279*` filters time out on the public endpoint, so classes are filtered in Python: people, places, schools, museums, government, sports teams, periodicals and similar are excluded (about 2,490 items, of which about 2,300 are kept).
2. Details, 50 organizations per query: label (en, else mul), description, P31, P856, P2002 (X), P2037 (GitHub), P571, P1128 (employees, with rank and point in time), P112 (founded by), P169 (CEO, with rank, start and end).
3. People named in P112/P169, 50 per query: label, description, P31, P856, P2002, P2037.

One raw record per organization (`external_id = <QID>`), holding all of its bindings and its people's bindings in canonical order. No cursor: it's a full refresh, and unchanged records are skipped by content hash.

**License / terms.** Data is CC0. The query service asks for a descriptive User-Agent and for clients to avoid parallel heavy queries.

**Rate.** One query at a time, sent as form POST (which WDQS recommends for long queries), 2 s between requests, backoff on 429/5xx/timeouts. A full refresh is about 2 + 2 × ⌈orgs/50⌉ ≈ 95 queries and took about 6 minutes live (2,290 organizations). During a WDQS outage the endpoint answered GET requests with `429 Retry-After: 1000` ("rate-limiting to 1 req / min"). The connector never retries sooner than asked: a requested wait over 5 minutes ends the run, and the job queue retries it later.

**Predicates.**
- company: `name`, `wikidata`, `description`, `website`, `domain`, `x_handle`, `github`, `founded_year` (earliest P571 year), `headcount` (best-ranked, latest P1128), `based_in` → place
- place: `name`, `wikidata`
- person (P31 = human only): `name`, `wikidata`, `description`, `website`, `x_handle`, `github`; edges `founded` → company (P112) and `ceo_of` → company (P169, with `valid_from` / `valid_to` from P580/P582)

**Identity rules.** A company's `domain` comes only from a website on the company's own host: the site root, or a page whose host matches the company name. It is not used when two in-scope items share it (Mozilla / Mozilla Corporation, GitHub / GitHub, Inc.). An X or GitHub handle shared by two in-scope items is likewise recorded as a fact but not used as an identity key. People carry their company as `context`.

**source_ref.** `wikidata:<subject QID>:<property|predicate>[:<object key>]`, e.g. `wikidata:Q7624104:P112:Q7146257`, `wikidata:Q21708200:P169:Q7407093:2023-11-22`.

---

## Hacker News launches — `hn`

**Fetched.** Algolia HN Search, `https://hn.algolia.com/api/v1/search_by_date?query="Launch HN"&tags=story&hitsPerPage=1000`. Algolia stops at 1,000 hits per query, so requests are windowed by `numericFilters=created_at_i<…`. The first run backfills everything (about 1,220 stories, 2 requests). Later runs read `created_at_i >= cursor − 7 days`, so points of recent launches are refreshed. Kept fields: objectID, title, url, author, created_at(_i), points, num_comments, story_text, _tags. One raw record per story (`external_id = story:<objectID>`).

**Cursor.** `{"created_at_i": <max seen>, "last_run": {"stories", "unparsed_titles"}}`, saved at the end of a complete run. A `--limit N` run takes the newest N stories and does not move the cursor.

**License / terms.** HN content is user-submitted. The Algolia HN API is public and needs no key. Rebox stores titles, URLs, authors' HN usernames and post text in the raw store. Only the parsed facts below become claims.

**Rate.** 1 s between requests, backoff on 429/5xx.

**Parsing.** Titles such as `Launch HN: Acme (YC W24) – Tagline` and the variants covered in `tests/connectors/test_hn.py` give company name, YC batch and tagline. Titles that are not a launch of a named company (Ask HN, "lessons from my Launch HN", bare descriptions, two companies in one title) stay as raw records and produce no claims. In the live data, 137 of 1,223 stories are not parsed.

**Company domain.** Taken from the story URL unless it points into a platform (github.com, youtube.com, producthunt.com, medium.com, …, `*.github.io`, …). Otherwise it is the first link in the post whose host matches the company name (`Nao Labs` → `getnao.io`, `Buy Me a Coffee` → `buymeacoffee.com`). Product subdomains are stripped (`app.`, `cloud.`, …). A GitHub owner comes from a github.com story URL, or from a post link whose owner matches the name. About 89% of parsed launches get a domain.

**Predicates.**
- launch: `name` (tagline, else company name), `hn_story`, `url` (unless only its query string makes it specific), `points`. All are dated `valid_from` = story time.
- company: `name`, `domain`, `github`, `yc_batch`; edge `launched` → launch. Context = [author].
- person (author): `name` (= HN username), `hn_user`; edges `posted` → launch and `founded` → company with `{"basis": "launch_hn_author"}`. Context = [company].

**source_ref.** `hn:story:<id>:<predicate>:<role>`; the author's own facts are `hn:user:<username>:<predicate>`, the same claim from every story.

---

## GitHub — `github`

**Enabled only with `GITHUB_TOKEN`.** Without it, `enabled()` is false, the scheduler skips it, and `fetch` refuses to run. No GitHub access existed when this was built, so it is tested only on synthetic fixtures (`fixtures/raw/github/`, logins like `example-octo-1`).

**Fetched.** `GET /search/users?q=location:"San Francisco" followers:<range> type:user` over follower ranges 100–149, 150–199, 200–299, 300–499, 500–999, 1000–2999 and ≥3000. Search returns at most 1,000 results per query, and a range above that is logged as truncated. Then `GET /users/<login>` for each, and `GET /orgs/<org>` for a `company` of the form `@org` (once per run; a 404 means no edge). One raw record per user (`external_id = user:<login>`) with its org.

**Cursor.** `{"last_login"}`, the last login written, so an interrupted or `--limit` run resumes. It resets after a complete pass.

**License / terms.** GitHub API terms. Only public profile fields are stored. Emails are never requested or stored.

**Rate.** 1 s between requests. A 403/429 with rate-limit headers sleeps until `Retry-After` / `x-ratelimit-reset` (capped at 15 min). Otherwise backoff.

**Predicates.**
- person: `name` (profile name, else login), `github`, `x_handle`, `website` (blog, if URL-like), `headline` (bio), `location`. Context = [company].
- company (only from `@org`): `name`, `github`, `website`, `domain` (not for platform hosts), `x_handle`.
- edge: person `worked_at` company (no dates). A plain-text company is ignored.

**source_ref.** `github:user:<login>:<predicate>[:<org>]`, `github:org:<login>:<predicate>`.

---

## X profiles — `x`

**Enabled only with `X_BEARER_TOKEN`.** This is enrichment, not discovery. It reads the `x_handle` values already in the graph (people and companies, from Wikidata, GitHub, …) through `DATABASE_URL` as `rebox_app`. It is tested on synthetic fixtures (`fixtures/raw/x/`, fake handles).

**Fetched.** `GET https://api.x.com/2/users/by?usernames=<up to 100>&user.fields=name,description,location,url,public_metrics,entities`. `entities` is needed because `url` is a t.co link and only `entities.url.urls[].expanded_url` holds the real site. One raw record per found profile (`external_id = user:<handle>`). Its payload also records which entity kinds carry the handle and whether they already have a name, so `extract` stays pure.

**Cursor.** `{"last_handle"}` after each batch. It resets after a complete pass.

**License / terms.** X developer terms. Public profile fields only.

**Rate.** 1 s between batches. A 429 waits until `x-rate-limit-reset`, and `x-rate-limit-remaining: 0` pauses before the next batch.

**Predicates** (on the existing entity, matched by `x_handle`): `x_followers`; `headline` (people) or `description` (companies); `location` (people); `website` (expanded, never t.co); `name` only if the entity has none. Posts and engagement are out of scope for v0.

**source_ref.** `x:<handle>:<predicate>:<kind>`.

---

## X following-graph spider — `x_spider`

**What.** Rebox's own crawler. It finds out who the best-connected SF people follow on X (people and companies) and ranks those accounts by how many seed accounts follow them. Code: `connectors/x_spider.py`, using the shared client `xapi.py`. Seeds: `config/seeds.yaml` → `seeds: {<set>: [<username>, …]}` (override the path with `SEEDS_FILE`). Each seed set is its own worker identity, `<set>-spider-<6 digits>`. `XSpider.workers()` lists them, and `XSpider.worker_for(record)` returns the seed set a `x_following` record belongs to (`payload.seed_set`; null for expansion pages). Starting sets are `dbooth: [davidbooth]`, `yoni: [yoni_michael]` and `sf-core: []`.

**Fetched**, in priority order. Each phase starts only when the one before it has finished:
1. Seeds: `GET /2/users/by?usernames=…`. Ids are cached in the cursor.
2. **Direct following of every seed:** `GET /2/users/:id/following?max_results=…&user.fields=description,location,url,entities,public_metrics,profile_image_url,verified_type,created_at`. Seeds are crawled round robin, and in each round the remaining user budget is split evenly between the seeds that are not finished yet. `max_results` = min(`X_SPIDER_PAGE_SIZE` (1000), that share). Pages resume through `pagination_token`, which is stored in the cursor.
3. **Posts**, only once every seed's following is complete: `GET /2/users/:id/tweets?exclude=replies,retweets&max_results=≤20` for the top-scored frontier accounts whose kind is in `X_SPIDER_POST_KINDS` (default `company`). Within a score, SF/Bay Area accounts come first.
4. **Depth-1 expansion**, only once every seed's following is complete: the top frontier *people* at depth 1 that look like SF people are crawled like seeds, until the budget is spent. "Look like SF people" means the location or bio mentions SF, the Bay Area, Berkeley, Oakland, Palo Alto, …, or there is no location and at least 2 seeds follow them. Crawled accounts are marked `fetched` in the frontier.

`X_SPIDER_PHASES` (default `following,posts,expansion`) restricts a run to some phases. Running only `posts`, for example, skips the "following first" gate.

**Spend guard (X is pay-per-use).** Estimated price: $0.01 per user object and $0.005 per post, charged on the objects actually returned (X sends no cost headers today). Limits:
- per run: `X_MAX_USER_READS_PER_RUN` (default 2000) and `X_MAX_POST_READS_PER_RUN` (default 1000);
- in total: `X_TOTAL_BUDGET_USD` (default 30). Cumulative estimated spend `{user_reads, post_reads, api_calls, usd}` is kept in `connector_state.cursor.spend` and written to the DB **after every call**, so a crash cannot lose it.

A request is sent only if its worst case (`max_results` objects) fits both limits. Page sizes shrink to fit the budget, and the run stops cleanly when nothing fits (`budget_stop`). The spider also stops at once, **without retrying**, on 401, 402, 429, a 403 that mentions a usage cap or credits, or `x-rate-limit-remaining: 0` (`x_stop`). Both are recorded in the run stats and in `cursor.last_run`. 5xx and network errors are retried 3 times with backoff. Protected or suspended accounts are skipped (`crawl[id].error`). Run stats (`XSpider.run_stats`, also stored in `cursor.last_run`) are `api_calls`, `cost_usd`, `user_reads`, `post_reads`, `x_total_spent_usd`, `x_total_budget_usd`, and `budget_stop` / `x_stop` / `posts_stop` / `waiting` / `expansion` when they apply. The connector has `scheduled = False`, so it runs only when someone runs `sync x_spider` or `enqueue x_spider`.

**Cursor.** `{"seeds": {username: id|"missing"}, "crawl": {x_id: {next, done, pages, users, depth, expansion?, error?, completed_at?}}, "posts_done": [x_id], "spend": {…}, "last_run": {…}}`. It is saved after every page. A `--limit` run keeps only the progress of its last complete page.

**Frontier** (`frontier`, `source='x_spider'`): `key` = X user id, `kind` = `person`|`company` (from `classify`), `label` = username, `payload` = the user object, `depth` = 0 for seeds (`discovered_by = ["seed-set:<set>"]`) and seed depth + 1 for accounts they follow. `discovered_by` = usernames of the accounts that follow it, and `score` = the number of those distinct accounts. Re-reading a page never inflates a score.

**Raw records** (payload shapes; the AI parser slice S9 consumes them):
- `x_user`, `external_id = user:<x id>`, url `https://x.com/<username>`:
  `{"type": "x_user", "user": <X user object as returned: id, username, name, description, location?, url?, entities? {url.urls[].expanded_url, description.urls[], description.mentions[]…}, public_metrics {followers_count, following_count, tweet_count, listed_count, like_count, media_count}, profile_image_url, verified_type?, created_at>}`
- `x_following`, `external_id = following:<seed x id>:<page no>`, url `https://x.com/<seed>/following`:
  `{"type": "x_following", "seed": <seed user object>, "seed_set": "<set>"|null, "depth": 0|1, "page": n, "users": [<user object>, …]}`
- `x_post`, `external_id = post:<post id>`, url `https://x.com/<author>/status/<id>`:
  `{"type": "x_post", "post": {id, text, created_at, lang, entities?, public_metrics, conversation_id, edit_history_tweet_ids}, "author": {"id", "username"}}`

**Classification** (`classify`, deterministic). An account is a `company` when `verified_type` is `business` or `government`, when the bio says "official account", or when company signals score ≥ 2 and outweigh person signals. Company signals: a company-like name ("… Labs/AI/Capital/Ventures/HQ…"), handle = name + ai/hq/labs/… (`@TectonAI` "Tecton"), the website is the account's own (host label = handle or name, `get-`/`try-` prefixes, `-ai`/`-labs` suffixes), "we/our/join us" voice, product phrases. Person signals: first person, pronouns, roles (founder, CEO, partner at, ex-…, prev), and a "First Last" name. Everything else is a `person`. On the live sample (148 accounts) 10 came out as companies.

**Predicates** (`x_user`; the subject is matched by `x_id`, then `x_handle`, then `domain` for companies):
- both kinds: `x_id`, `x_handle`, `name` (display name without emoji), `avatar` (`profile_image_url` at 400×400; the default avatar is dropped), `x_followers`
- person: `headline` (bio with t.co links expanded), `location`, `website` (`entities.url` expanded, never t.co)
- company: `description` (bio), plus `website` and `domain` only when the site is the company's own
- `x_following` → `follows` edges seed → account (`object_kind '*'`, no props). Edge refs carry no name, so the account's own `x_user` record names it. `x_post` produces no claims.

**source_ref.** `x_spider:user:<x id>:<predicate>`; `x_spider:follows:<seed x id>:<x id>`.

**License / terms.** X developer terms. Public profile fields and public posts only. Fixtures (`fixtures/raw/x_spider/`) are real public accounts, trimmed to the fields above, with 13 accounts per seed page.

---

## Exa discovery — `exa`

**What.** Fresh SF startup activity found through Exa web search: recent launches, seed and Series A announcements, YC launches, and named startups the architect lists. Code: `connectors/exa.py`. Queries: `config/seeds.yaml` → `exa.queries`. Each query is a string or `{q, category?, days?, num? (≤ 25), include_domains?}`.

**Fetched.** `POST https://api.exa.ai/search` with header `x-api-key`. Request fields: `numResults` ≤ 25, `type: auto`, `contents: {text: {maxCharacters: 3000}, highlights, summary}`, `startPublishedDate` = now − `days` (default 30), and `category` / `includeDomains` when the query sets them. A query with `include_domains` gets no date filter unless it sets `days`; the `company`/`people` categories never get one, because Exa rejects it with a 400. A 400 skips that query (`failed_queries` in the stats) and the run continues. 429/5xx are retried. Budget: `EXA_MAX_REQUESTS_PER_RUN` (default 100) requests per run, round robin over the queries (`cursor.next_query`). The cost Exa reports (`costDollars.total`) is summed into the run stats as `cost_usd`, together with `api_calls`, `results`, `homepages` and `queries`.

**Raw record** `exa_result`, `external_id = url:<normalized url>` (a page found by two queries in one run is stored once):
`{"type": "exa_result", "query", "category", "url", "url_key", "domain", "homepage": bool, "title", "published_date", "author", "text" (≤ 3000 chars), "highlights": [str], "summary", "image", "favicon"}`.
`homepage` = the URL is the root (or a language root) of a host that is not a platform, news site or directory. The parser slice extracts articles.

**Frontier** (`source='exa'`): one row per homepage domain. `kind` = `company`, `label` = cleaned name, `discovered_by` = `exa:<query>`, `score` = the number of distinct queries that found it.

**Predicates** (homepages only; articles produce none): company `domain` (identity), `name` (the title segment that matches the host, without "(YC S26)", e.g. "Ditto | Your college matchmaker" on ditto.ai → "Ditto"), `website` (site root), `yc_batch` when the title carries one. **source_ref** `exa:domain:<domain>:<predicate>`.

**Named startups** (checked by hand in Nov 2026): Ditto → `ditto.ai` (AI matchmaker for college students; $9.2M seed led by Peak XV); Known → `known.com` (swipeless dating with a short voice call; X @dateknown; $9.7M seed, Dec 2025; formerly knowndating.com, which Exa does not index).

**License / terms.** Exa API terms. The raw store keeps titles, URLs, short text excerpts and highlights. Fixtures (`fixtures/raw/exa/`, 20 results) are trimmed to 600 characters of text, with email addresses redacted.

