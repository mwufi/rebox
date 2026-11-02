# S14 — Coverage checker: "how much of this list does Rebox know?"

**Goal:** given any list of people/companies (a CSV exported from Cosign, a portfolio sheet, a conference attendee list), report which ones Rebox already has, how well (how many facts, which sources), and which are missing — and optionally queue the missing ones for the indexers. This is how we measure the graph against a reference without scraping anyone.

**Branch:** `s14-coverage`  **DB prefix:** `s14_`  **Contract:** CONTRACTS §4 (normalizers), §5 resolver order (read-only use), §13 frontier.

## Owns
`apps/worker/rebox_worker/coverage.py`, `apps/worker/tests/test_coverage.py`, `fixtures/coverage/*.csv`, `docs/COVERAGE.md`. Minimal edits to `cli.py` registration (another branch, S11, also adds commands there — keep your edit to a few lines so the merge is trivial).

## Input
CSV with a header; recognised columns (case-insensitive, any subset): `kind` (person|company; default guess: company if `domain`/`website` present and no person-ish column, else person), `name`, `x` / `twitter` / `x_handle` (handle or x.com URL), `github`, `domain`, `website` / `url`, `linkedin` (kept and echoed, not matched — Rebox has no LinkedIn key), `company` (for people: helps fuzzy disambiguation). Unknown columns are passed through to the output.

## Matching (read-only; never writes claims)
1. Hard keys first, normalised exactly as the resolver does (reuse `normalize.py` and the resolver's lookup helpers — do not reimplement): `x_handle`, `github`, `domain` (from `domain` or `website`), `wikidata`.
2. Then name: exact folded-name match within the kind, then trigram (`search_name`) ≥ 0.6; if `company` is given for a person, prefer candidates with a `worked_at`/`founded` edge to a company of that name. Ambiguous (≥ 2 candidates within 0.05 of each other) → status `ambiguous` with the candidates listed.
3. Status per row: `matched` (hard key), `matched_name`, `ambiguous`, `missing`.
4. For matched rows, report: entity id, folded name, number of facts, number of edges, sources (layer list), has timeline events (y/n), explorer URL (`$EXPLORER_URL/e/<id>`, default `http://localhost:3000`).

Runs as `rebox_app` with public layers visible (same view setup as the pipeline). Must handle 10k rows in < 60 s (batch the hard-key lookups with `= any($1)`).

## Output
`rebox-worker coverage FILE.csv [--out report.csv] [--json] [--enqueue]`
- Writes `report.csv` (input columns + `status, entity_id, rebox_name, facts, edges, sources, timeline, url, candidates`).
- Prints a summary: totals and % by status and kind; "thin" matches (≤ 3 facts); top sources contributing to matches.
- `--enqueue`: rows that are `missing` (or thin) and have an `x_handle` go into `frontier` (`source='x_spider'`, `discovered_by=['coverage:<file stem>']`, score 1.0, status `new`); rows with only a domain/name go into a frontier entry with `source='exa'` (key = domain or name) for the Exa connector to look up. Idempotent (upsert, merge `discovered_by`). Print how many were queued. Don't run any fetches.

## Verification
- Tests on a fixture DB: each match path (x handle in URL form, `@Handle` case, domain from `https://www.ditto.ai/`, github, exact name, trigram name, ambiguous name, missing); person-with-company disambiguation; unknown columns pass through; `--enqueue` upserts and is idempotent; no claim/change rows are written.
- A fixture CSV of ~30 rows mixing hits and misses (synthetic people only; real well-known companies are fine).
- Live (read-only): run against a copy of `rebox_live` named `s14_live` (`createdb -T rebox_live s14_live` as postgres — **never** connect to `rebox_live` itself) with a CSV you write of ~40 well-known SF startups/founders (e.g. Ditto, Known, Brex, Ramp, Vercel, Linear, Notion, Figma, Anthropic, OpenAI, Scale AI, Perplexity, Cursor/Anysphere, Mercury, Retool, Rippling, Deel, Plaid, Stripe, and founders by X handle); paste the summary and the misses.
- `docs/COVERAGE.md`: how to run it in the compose stack (`docker compose cp list.csv worker:/tmp/list.csv`, `docker compose exec worker rebox-worker coverage /tmp/list.csv --out /tmp/report.csv`, `docker compose cp worker:/tmp/report.csv .`) and a note that Rebox does **not** fetch from Cosign — the list comes from an export the user is permitted to use.
