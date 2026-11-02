# Sources

What each public connector fetches, under which terms, how politely, how often, into which layer, and which predicates it writes. Code: `apps/worker/rebox_worker/connectors/<name>.py`. Fixtures: `fixtures/raw/<name>/`.

Every connector sends `USER_AGENT` (default `rebox-bot/0.1`; set it to `rebox-bot/0.1 (+https://github.com/rebox)` or similar, and never to a personal email). Each one waits between requests and backs off exponentially on 429/5xx, honouring `Retry-After` / rate-limit reset headers. It supports `--limit N` for dev runs. `extract` is pure and stable. Identity facts (`wikidata`, `domain`, `github`, `x_handle`, `hn_user`, `hn_story`) are written as ordinary claims. No connector writes a `meta.private` predicate.

| Source | Layer | Schedule (`interval_s`) | Needs |
| --- | --- | --- | --- |
| Wikidata | `public/wikidata` | weekly (604800) | nothing |
| Hacker News launches | `public/hn` | hourly (3600) | nothing |
| GitHub | `public/github` | weekly (604800) | `GITHUB_TOKEN` (disabled without it) |
| X profiles | `public/x` | weekly (604800) | `X_BEARER_TOKEN` (disabled without it) |

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
