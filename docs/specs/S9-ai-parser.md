# S9 — AI parsing with Haiku: bios and posts → evidence-backed claims

**Goal:** turn the free text Rebox already fetches (X bios, X posts, GitHub bios, Exa articles) into typed claims — current role and company, past companies, location, founding, and timeline milestones (launches, fundraises, hires, acquisitions) — each with a quoted evidence span, cheaply, replayably and safely.

**Branch:** `s9-parser`  **DB prefix:** `s9_`  **Contract:** CONTRACTS §8 (Connector), §12 (evidence required for parser workers), predicates incl. `milestone`, `acquired`.

## Owns
`apps/worker/rebox_worker/parsers/**` (shared LLM client, prompts, schemas), `apps/worker/rebox_worker/connectors/{x_parse,github_parse,exa_parse}.py`, `apps/worker/tests/parsers/**`, `fixtures/raw/{x_parse,exa_parse}/**`, `docs/PARSING.md`, registry lines, `anthropic` dependency in `apps/worker/pyproject.toml` + `uv.lock`.

## Design (fits the existing Connector protocol — no pipeline changes)
- A parser is a connector whose **fetch** reads upstream raw records (`x_spider`'s `x_user` and `x_post`, `github` profiles, `exa` results) that it hasn't parsed yet (cursor = upstream raw `fetched_at` + key; also re-parse when the upstream `content_hash` changed), calls the model, and yields a RawRecord `{input_ref, input_hash, model, prompt_version, output}`. So model output is itself stored raw: **replay never calls the model again**, and a new `prompt_version` re-parses deliberately.
- **extract** is pure: model output → ClaimInputs, each with `evidence` (`url` of the post/profile, `author`, `quote`). Discard any claim whose quote is not an exact substring of the input text (hallucination guard).
- Model: `PARSER_MODEL` (default `claude-haiku-5-5`) via the official `anthropic` SDK, structured output through a single tool with a JSON schema, a cached system prompt, `max_tokens` small. Budget `PARSER_MAX_CALLS_PER_RUN`; record `api_calls`, input/output tokens and `cost_usd` in run stats.
- Batch several short inputs per call (e.g. 10 bios) when it lowers cost without hurting accuracy; measure.

## What to extract
- **Bios (X, GitHub):** current roles `{title, org}` where org is an `@handle` (→ company EntityRef with identity `x_handle`) or a name (→ company EntityRef by name with the person as context); founder status (`founded`); past orgs from "ex-", "prev", "formerly" (`worked_at` with `valid_to` = observed date, title optional); `based_in` place (normalize "SF", "San Francisco", "Bay Area" → San Francisco place, identity `wikidata: Q62`; other cities by name); investor signals ("investing at @x", "partner @fund", "angel") → `worked_at` with title.
- **Posts and Exa articles:** milestones `{title, kind ∈ launch|funding|hire|acquisition|product|award|other, url}` dated by the post date, attached to the company (and person when first-person); `acquired` edges; "welcome @a to @b" → `worked_at(a → b, valid_from = post date)`. Skip opinions, jokes and ads (return nothing).
- **Never extract:** emails, phone numbers, personal addresses, or anything about private individuals who are only mentioned (keep subjects to the account owner, the companies named, and people identified by @handle).
- Layers: `public/x_ai`, `public/github_ai`, `public/exa_ai`. Workers: `xbio-parser-*`, `xpost-parser-*`, `exa-parser-*`, `github-parser-*` (kind `parser`).

## Verification
- Offline tests with recorded model outputs (no network): schema validation, evidence-substring guard drops fabricated quotes, @handle → company ref, ex-/prev → ended roles, SF normalization, milestones from posts (≥ 10 realistic post shapes incl. "welcome @x to @y", "we raised", "launching today", jokes → nothing), privacy filter (emails/phones never emitted), replay makes zero model calls.
- A small **eval**: 40 labelled inputs (write them by hand from public-style examples; synthetic people only) → precision/recall per field for the chosen prompt; report numbers and cost per 1,000 inputs.
- Live smoke against `s9_live` (copy `s4_live`, migrate to latest, and add a few hundred real `x_user`/`x_post` raw records by running `x_spider` from `main` if merged, otherwise from fixtures): `sync x_parse --limit 200`; report claims by predicate, review items, cost, and 5 before/after entity views (bio text vs extracted facts).
- Secrets: source `/home/claude/rebox/secrets/keys.env` for live runs only; never print/log/commit key values.
