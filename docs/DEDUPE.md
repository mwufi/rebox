# Dedupe pass

The resolver matches incoming facts to existing entities by hard keys (CONTRACTS §8), but a key can
arrive *after* an entity was minted: an article mints "Figma" by name, Wikidata later mints "Figma,
Inc." by QID with `domain: figma.com`, and a third source adds `figma.com` to the first one. The bio
parser (`public/x_ai`) also mints companies it only knows by name ("SWE at Google") once per batch.
`rebox-worker dedupe` cleans this up: it merges what is clearly the same thing and queues what is
only likely the same thing for a human.

It never writes graph tables itself. Every merge is `POST /v0/admin/merge {from, into}` with the
admin token, so `merge_log` and the change feed (`change` op `merge`) are written by the one place
that owns merges, and every merge can be undone with `POST /v0/admin/split`. Review items go to
`review_item` with `kind = 'possible_duplicate'` and show up in `GET /v0/review`.

## What is merged automatically, what is reviewed

The pass reads as `rebox_app` with all public layers visible and follows `merged_into` chains (an
entity counts with everything already merged into it). Three phases, in order:

| phase | finds | decision |
| --- | --- | --- |
| **1. shared key** | entities of the same kind sharing a value of `wikidata`, `domain`, `github`, `x_handle`, `x_id` or `hn_user` (groups that share members are unioned) | **merge**, unless two members have *conflicting strong keys* → **review** |
| **2. name-only companies** | companies with no `wikidata/domain/github/x_handle/x_id/hn_user/website/url` claim at all, by folded name | one keyed company with that name → **merge** into it; several → **review** (no merge); none → **merge** the name-only ones into the one with most claims |
| **3. name + neighbor** | same kind, same folded name, and an edge (either direction) to the same entity | always **review**, reason `name+neighbor` |

**Conflicting strong keys** (phase 1): two members with different values of `wikidata` or `x_id`
(any kind), or of `github` or `x_handle` (people). Several `domain`s or X handles on one company are
normal and do not block a merge. Two "Figma" entities with different QIDs are reviewed, not merged.

**Target** of a merge: the member with a `wikidata` claim, else the one with most claims, tie → the
oldest `created_at`. Phase 2 merges name-only companies into the keyed one; without one, into the
name-only company with most claims (tie: oldest).

**Folded name** (phases 2 and 3): lowercase, trimmed, a trailing `(...)` dropped, whitespace
collapsed; companies also drop trailing legal forms (`inc`, `llc`, `ltd`, `corp`, `corporation`,
`co`, `gmbh`, `sa`, with or without `.`/`,`). Exact equality only, never trigram similarity. A
name-only company is matched by its main names: the one most of its name claims use, plus any other
used by at least a third of them (a stray second name such as "Glass" on "Ramp" does not chain two
companies together). Folded names shorter than two characters are ignored.

**Never automatic:**
- people are never merged by name (phase 2 is companies only);
- phase 3 never merges;
- for people, a shared `place` (half of them are `based_in` San Francisco) and `follows` edges are not
  shared neighbors; for companies a shared city counts (two "OpenAI"s in San Francisco are worth a
  look);
- a group in which an admin **split** two members (`merge_log` op `split`), or that contains two
  members of a **dismissed** `possible_duplicate` item, is reviewed instead of merged. Undo sticks.

Review items are not reopened: an open or dismissed `possible_duplicate` item for the same set of
entities (sorted ids) suppresses a new one, also within one run. A second run therefore finds nothing
new to merge or review.

## Run it

Dry run first. It prints every group, the decision and the target, and writes nothing (no merge,
review item, worker or run):

```sh
docker compose exec worker rebox-worker dedupe --dry-run
```

A real run needs the admin token in the worker's environment (the worker service only has the
ingest token):

```sh
docker compose exec -e ADMIN_TOKEN="$ADMIN_TOKEN" worker rebox-worker dedupe
```

Options: `--kinds company,person` (default; other kinds use the `wikidata`/`x_id` conflict rule),
`--limit N` (act on at most N groups, merges or new reviews; the rest wait for the next run),
`--json` (the whole report as JSON instead of one line per group).

Outside compose, from `apps/worker`:
`DATABASE_URL=<app-role URL> API_URL=... INGEST_TOKEN=... ADMIN_TOKEN=... uv run rebox-worker dedupe`.

Each real run is recorded as worker `dedupe-enricher-<6 digits>` (kind `enricher`, source `dedupe`)
in a run of kind `enrich`, with stats:

| stat | meaning |
| --- | --- |
| `groups` | phase-1 groups (after union) |
| `auto_merged` | entities merged away in phase 1 |
| `skipped_conflict` | phase-1 groups reviewed instead of merged (conflict, split or dismissal) |
| `name_only_merged` / `name_only_groups` | phase 2 merges / decisions |
| `name_neighbor_groups` | phase-3 groups |
| `review_opened` / `review_existing` | new review items / ones already open or dismissed |
| `merge_errors` | merges the admin API refused (e.g. merged concurrently); the run carries on |
| `deferred` | groups left for the next run by `--limit` |

## Undo a merge

```sh
curl -X POST "$API_URL/v0/admin/split" -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H 'Content-Type: application/json' -d '{"id": "<the merged-away entity id>"}'
```

The entity is un-merged, the split is logged in `merge_log` and the change feed, and later dedupe runs
open a review item for the pair instead of merging it again. To find what a run merged:
`select from_id, into_id, at from merge_log where op = 'merge' and actor = 'admin' order by at` (the
run's start and end times are in `run`). Dismiss a review item with
`POST /v0/admin/review/:id/dismiss`; merge one with `POST /v0/admin/merge {from, into, review_id}`.

## Accounts queued by handle (x_spider `lookup` phase)

`rebox-worker coverage --enqueue` queues missing X accounts as frontier rows keyed `@<handle>`
(`source = 'x_spider'`). The X spider's first phase, `lookup`, resolves them in batches of up to 100
(`GET /2/users/by`, highest score first): each account found becomes an ordinary `x_user` record and
a frontier row keyed by its user id (depth 0, `discovered_by` carried over), and the `@` row becomes
`fetched` (payload gains `x_id`) or `failed` with `payload.lookup_error` (`not found: ...`,
`suspended: ...`, `invalid: ...`). It is charged against the same spend guard as every user read
(`X_TOTAL_BUDGET_USD`, `X_MAX_USER_READS_PER_RUN`) and stops cleanly at the budget; rows it could
not afford stay `new` for the next run. `X_SPIDER_PHASES=lookup` runs only the lookups (no seed or
following calls); the default phases are `lookup,following,posts,expansion`.
