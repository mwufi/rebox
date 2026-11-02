# AI parsing (S9)

Rebox already stores free text it fetched: X bios and posts (`x_spider`, `x`), GitHub profiles (`github`) and Exa articles (`exa`). The parsers turn that text into typed claims with Claude Haiku. Every claim carries a quoted evidence span. Parsing is cheap, can be replayed, and is guarded against hallucination and privacy leaks.

## Connectors and workers

| connector | reads (upstream raw records) | layer | worker (CONTRACTS §12) |
| --- | --- | --- | --- |
| `x_parse` | `x_spider` / `x` user objects (bios) | `public/x_ai` | `xbio-parser-NNNNNN` |
| `x_parse_posts` | `x_spider` `x_post` records (incl. the author's own thread continuations) | `public/x_ai` | `xpost-parser-NNNNNN` |
| `github_parse` | `github` profiles (bio, plain-text company, location) | `public/github_ai` | `github-parser-NNNNNN` |
| `exa_parse` | `exa` `exa_result` articles (not homepages) | `public/exa_ai` | `exa-parser-NNNNNN` |

All four are `parsers/base.py:ParserConnector`. Each sets `worker_kind = "parser"`, so its claims are written with the parser default trust of 0.6, and the writer requires `evidence` on every one.

## How a parse works

1. **fetch** pages through `raw_record` rows of the upstream sources in `(fetched_at, source, external_id)` order, starting after the cursor. When the spider re-fetches a record whose content changed, the record gets a new `fetched_at` and comes round again.
2. Each upstream record becomes an `InputDoc` (`parsers/inputs.py`): the fields the model sees (`name`, `location`, `bio`, `company`, `text`, `title`) plus meta (handle, X id, post URL and date). The model never sees anything else, and quotes are checked against exactly these fields.
3. An input is skipped if this parser already stored an output for the same input hash, `PROMPT_VERSION` and model. Changing `PROMPT_VERSION` or `PARSER_MODEL` resets the cursor, so everything is re-parsed on purpose.
4. Inputs go to the model in batches (`PARSER_BATCH`; defaults are 10 bios, 8 posts, 4 articles) through **one strict tool** (`record_facts`, a JSON schema in `parsers/schema.py`) with forced `tool_choice`. The system prompt is cached (`cache_control`), so after the first call each call reads the ~3.5k-token prefix from cache.
   - If the answer is truncated or missing, the batch is split in half and retried.
   - An input that still fails is stored with `output: null` and counted in `failed_inputs`.
5. **The model output is stored as a raw record**: `{input_ref, input_hash, model, prompt_version, input, output, graph}`. `graph` is what the graph already knew about the identities and names the answer mentions, looked up at fetch time.
6. **extract** (`parsers/extract.py`) is pure: stored output → `ClaimInput`s with `evidence = [{url, author, quote, observed_at}]`.
   - **Replay never calls the model.** `rebox-worker replay x_parse` re-extracts from the stored outputs.

Budgets and usage:
- `PARSER_MAX_CALLS_PER_RUN` (default 2000) and `--limit N` (inputs) stop a run. The cursor points at the last parsed input.
- After each call, usage (`api_calls`, input / output / cache tokens, `cost_usd`, `failed_inputs`) goes to `ctx.add_usage`, so it lands in the run's stats.

## What is extracted

| text | facts → claims |
| --- | --- |
| bios | role at an org → `worked_at` (value `{title}` if the title is in the text); past roles ("ex-", "prev", "formerly", "alum") → `worked_at` with `valid_to` = observed date; founder → `founded`; investor ("investing at @x", "Partner @fund", "GP at") → `worked_at` with the written title or `Investor`; city → `based_in` |
| posts | milestones `{title, kind ∈ launch, funding, hire, acquisition, product, award, other; url}` dated by the post (`valid_from`), on the company (and on the author when first-person); `acquired` edges; "welcome @a to @b" / "I'm joining @b" → `worked_at(a → b, valid_from = post date)` |
| articles | milestones and `acquired` for the companies the article names (never people) |

Opinions, jokes, threads, hiring ads, giveaways, departures and teasers give nothing.

Entities:
- **Account owner:** identity `x_id` + `x_handle` (X) or `github` (GitHub). Its kind (person/company) is the one the graph already has. An account the graph does not know uses the model's `account` guess (`other` → no facts), and gets its identity and `name` claims written with evidence.
- **Organizations by `@handle`:** company with identity `x_handle` (X), or `github` (GitHub bios, where `@org` is a GitHub org).
- **Organizations by name:** the graph's single company with that exact name if there is one. Otherwise a name-only company with the account as `context`, so the resolver's scoped fuzzy pass can match it. In articles, a company whose name matches the article's host gets `domain` identity.
- **Places:** cities from `parsers/places.py` (60 cities with Wikidata ids). "SF", "San Fran", "Bay Area", "SF Bay Area" → San Francisco (`wikidata: Q62`), "NYC" → New York City, and so on. Other places are dropped. For a move ("NYC → SF") only the destination is kept.
- Every new entity gets a `name` claim from `extract` (with evidence), so the resolver never adds an evidence-less name claim.

## Guards (in `extract`)

- **Evidence:** a fact whose `quote` is not an exact substring of the input text is dropped. A quote that starts with a field label the model echoed ("location: SF") is accepted without the label.
- **Grounding:**
  - Organization names must appear in the text.
  - `@handles` must be written in the text. An invented handle falls back to the written name, or the fact is dropped.
  - Titles not in the text are dropped; the role is kept.
  - "co-founder & CEO" becomes `founded` plus the title `CEO`.
- **Relevance (articles, S18):** Exa returns listicles, weekly roundups and foreign aggregators that name a company once. An article milestone is kept only when the article is *about* its company (`parsers/relevance.py`):
  - the company's name is in the article title, or in the first 400 characters of the text; and
  - the milestone's quote names the company, or the title does.

  Names match on word boundaries (only Latin letters and digits count as word characters, so "Dittoが" names Ditto and "Unknown" does not name Known). A lowercase occurrence of a capitalized name is not the name ("a different approach" is not "Different"). A trailing legal form or parenthetical is optional. Dropped milestones are counted as `irrelevant_dropped` in the run stats. One article also never gives the same milestone twice: identical (kind, normalized title, date) → one claim (`duplicate_milestones`). Both are post-filters on the stored output, so `rebox-worker replay exa_parse` applies them with no model calls. A replay does not retract claims written before the filter existed (see below).
- **Privacy:** facts whose quote, title or names contain an email address or a phone number are dropped. People other than the account owner appear only when written as an `@handle`, never by name. The account's own evidence quote is its display name, not its whole bio.

## Eval

`tests/parsers/eval_set.py` has 40 hand-labelled synthetic inputs: 16 X bios, 4 GitHub bios, 14 posts, 6 articles, 9 of which should produce nothing. Run it live with `uv run python tests/parsers/eval_parser.py [--record] [--batch N]`, or offline with `--replay`. Scores are on what `extract` emits (model plus guards). `worked_at` is scored on (org, current/past); title accuracy is reported separately.

Prompt `2026-11-02.1`, `claude-haiku-5-5`, batch 10 (recorded in `fixtures/raw/x_parse/eval/outputs.json`):

| field | precision | recall |
| --- | --- | --- |
| worked_at (current) | 1.000 | 1.000 |
| worked_at (past) | 1.000 | 1.000 |
| founded | 0.833 | 1.000 |
| based_in | 1.000 | 1.000 |
| milestone | 1.000 | 1.000 |
| acquired | 1.000 | 1.000 |
| hire (`worked_at` from posts) | 1.000 | 1.000 |
| **all** | **0.986** (71 TP, 1 FP) | **1.000** (0 FN) |

- Inputs that should produce nothing came out clean: 9/9.
- Exact titles: 23/27.
- The only error: "2x founder (sold to Acme)" → `founded Acme`.
- Cost: **$0.083 per 1,000 inputs** (7 calls, 4.7k uncached input + 24.5k cached + 5.2k output tokens for 40 inputs).

Batch size, measured with an earlier prompt revision (overall precision / recall, cost per 1k inputs):

| batch | precision | recall | cost per 1k |
| --- | --- | --- | --- |
| 1 | 0.97 | 0.97 | $0.133 |
| 5 | 0.96 | 0.93 | $0.093 |
| 10 | 0.96 – 1.00 | 0.94 – 0.99 | $0.079 – 0.090 |
| 20 | 1.00 | 0.94 | $0.077 |

The differences are within run-to-run noise, so batching does not hurt accuracy; it saves ~40% of the cost. Batch 10 is the default.

On real data (the live smoke):
- 145 X bios cost $0.081 per 1k, because real bios are longer.
- 142 Exa articles cost $0.20 per 1k, because article text is up to 4k characters.

## Operating

```
ANTHROPIC_API_KEY=... rebox-worker sync x_parse --limit 200     # bios
rebox-worker sync x_parse_posts                                # posts
rebox-worker sync exa_parse                                    # articles
rebox-worker replay x_parse                                    # re-extract, no model calls
```

A replay writes what extract emits now; it does not retract claims an older extract wrote and the new one no longer emits (e.g. milestones the relevance filter drops). To remove those, retract them as the same worker: for each `public/exa_ai` milestone claim whose `(subject, source_ref)` the current extract no longer produces, write the same claim with `"retract": true` (CONTRACTS §5) as the `exa-parser-*` worker.

Env: `ANTHROPIC_API_KEY` (parsers are disabled without it), `PARSER_MODEL` (default `claude-haiku-5-5`), `PARSER_MAX_CALLS_PER_RUN` (default 2000), `PARSER_BATCH` (optional; overrides the per-connector default).

To change the prompt:
1. Edit `parsers/prompt.py` and bump `PROMPT_VERSION`.
2. Re-run the eval and record it (`--record`).
3. Re-record the test fixtures with `tests/parsers/record_fixtures.py`.
