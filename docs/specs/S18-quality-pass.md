# S18 — Quality pass: mention-only merges, clustered milestones, relevant articles

**Goal:** fix the three data-quality problems found by spot-checking Ditto and Known on the live graph, so a company page reads like a clean history.

**Branch:** `s18-quality`  **DB prefix:** `s18_`  **Contract:** CONTRACTS §12 (Fact `support`), §14 (timeline).

## Owns
`apps/worker/rebox_worker/dedupe.py` (+ tests, `docs/DEDUPE.md`), `apps/worker/rebox_worker/parsers/**` and `connectors/exa_parse.py` (relevance only, + tests, `docs/PARSING.md`), `apps/api/src/timeline.ts` (+ tests), `apps/web/src/components/Timeline.tsx`, `apps/web/src/lib/timeline.ts` (+ tests). Propose §14 wording in the report. Don't touch resolve.py/ids.py/names.py/rawstore.py/writer (S16 in flight), identity.py/client.py/exchange.py/migrations (S17 in flight).

## Findings
1. **Wrong name-only merges.** Dedupe phase 2 merged HN's "Ditto" (YC W20, `yc_batch` + `launched`, no identity key) into ditto.ai, and HN's "Homebase" (W21, Southeast Asia rent-to-own) into joinhomebase.com. Those were real, separately-sourced companies, not bio mentions. (Both have been split on live by the architect; splits are sticky.)
2. **Milestone spam.** Known has 25 `milestone` facts from `public/exa_ai`: the same "$9.7M raise" from ~10 outlets (each article → its own milestone, slightly different titles), launch duplicates, and the same article yielding funding + launch twice.
3. **Irrelevant articles.** Some Exa results only mention the company in passing (a listicle about SF dating culture, a foreign aggregator) and still produce milestones attached to it.

## Build
1. **Dedupe: mention-only.** In phase 2, a name-only company is a merge candidate only if *all* its claims are in parser layers (`layer_id like 'public/%\_ai'`, i.e. it exists only because a parser mentioned it). Name-only companies with claims from any other layer go to a `possible_duplicate` review (`reason: name_only_structured`) when a safe keyed target exists, else are left alone. Applies to "among themselves" merges too (only mention-only entities converge). Tests: HN-style entity with `yc_batch` + same name as safe keyed target → review not merge.
2. **Timeline clustering (API).** Group `milestone` events of the same entity and same `kind` (launch/funding/hire/acquisition/product/award/other) whose dates are within 45 days of the cluster's first event and whose titles are similar (token Jaccard ≥ 0.3 after lowercasing/stopwords, or same amount token like `$9.7m`/`9.7 million` normalized). A cluster becomes ONE event: date = earliest, title = the title from the most-trusted/most-specific source (prefer one containing an amount; else the longest ≤ 120 chars), `sources: [{url, title, layer}]` (all members, de-duplicated by url), `support` = number of distinct urls. Non-milestone events are untouched. Deterministic; stable keys (`milestone-cluster|<first member key>`). Paging works on clustered events.
3. **Timeline UI.** A clustered milestone shows the title, date, kind icon, and "N sources" that expands to the source links. Single-source milestones look as today.
4. **Relevance in exa_parse.** Milestones are emitted only when the article is *about* the company: the subject company's name must appear in the article title or in the first 400 characters of the text, and the milestone's quote must name the company (or the article title does). Otherwise drop (count `irrelevant_dropped` in run stats). Bump `prompt_version` only if you change the prompt; prefer a pure post-filter in `extract` so a **replay** applies it to stored model output with zero model calls. Add a dedupe at extract time too: identical (kind, normalized title, date) from the same article → one claim.

## Verification
- worker suite (`S2_OWNER_URL/S2_APP_URL` → `s18_pytest`) + ruff; api (`S1_TEST_DB=s1_s18`) typecheck + tests; web typecheck + tests + build.
- Tests: mention-only rule (incl. Ditto/Homebase-shaped fixtures); clustering (Known-shaped fixture: 10 "raises $9.7M" variants over 3 weeks → 1 event with 10 sources; a launch and a funding on the same day stay separate; two fundings 6 months apart stay separate); relevance filter (listicle passing mention → dropped; on-topic article kept); extract-time dedupe.
- Live, on a copy only (`createdb s18_live && pg_dump rebox_live | psql -q s18_live`; **never** write to `rebox_live`), API from your worktree on port 3318: `replay exa_parse` into the copy (no model calls — do not set ANTHROPIC_API_KEY), then show Known's and Rocker's and Different's timelines before/after (event counts and the clustered events), and dedupe dry-run stats before/after the mention-only rule. Playwright screenshot of Known's entity page timeline into `.screens/` (light mode is enough).
- Secrets: none needed; never print/log/commit secrets.
