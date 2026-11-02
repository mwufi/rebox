# Rebox — status

_Last updated: 2026-11-02 (overnight build)._

## What exists

A working v0 of the open graph. It has:

- **The data model.** Entities, claims, layers and predicates, with RLS-isolated layers and deterministic ids.
- **One write path.** All writes go through `POST /internal/claims`, attributed to a **worker** inside a **run**, with **evidence**.
- **Spiders, connectors and parsers** that fill the graph. Parsers' model output is stored raw, so a replay never calls the model again.
- **Maintenance commands:** dedupe, coverage, export/import between instances, raw reindex.
- **An explorer:** search, entity pages with timeline, wiki and provenance, graph, query, review, and workers.
- **Grafana dashboards.**

### Slices merged

| Slice | What |
| --- | --- |
| S1–S6 | API core, worker core, infra, connectors (Wikidata, HN, GitHub, X), explorer, integration |
| S7 | Provenance: workers, runs, evidence, support; workers UI; Grafana |
| S8 | X following spider + Exa connector, frontier, spend guard |
| S9 | Haiku parsers: X bios and posts, GitHub, Exa articles → evidence-backed claims (precision 0.986 and recall 1.0 on the eval) |
| S10 | Timelines, generated wikis, explorer polish |
| S11 | Export/import bundles through the target's resolver (`docs/MERGE.md`) |
| S12 | Normalizer parity, checked against 97 shared cases and 360k fuzzed inputs; per-claim writer rejections |
| S13 | Hub performance: paged fold; SF and davidbooth pages went from 166–392 ms to 31–36 ms p95 |
| S14 | Coverage checker (`docs/COVERAGE.md`) |
| S15 | Dedupe pass, plus X handle lookups for queued accounts (`docs/DEDUPE.md`) |
| S16 | Resolver convergence: company names with a safe-target guard, `x_id` key, raw reindex, per-claim DB errors |
| S17 | Source trust table (`config/trust.yaml`), legacy claim workers (migration 0008) |
| S18 | Quality: mention-only merges, clustered milestones with sources, article relevance, handle identity claims |
| S19 | Follow-ups: import uses table trust, `replay --retract-stale`, parser domain/wikidata identity claims, cleaner entity sidebar |

Suites on main: worker 816, api 230, web 43. All green.

## The overnight graph (`rebox_live`, cloud workspace)

| | Count |
| --- | --- |
| Entities | 10,718: 5,295 companies, 4,285 people, 1,086 launches, 52 places |
| Claims | 64,221, of which 9,682 carry quoted evidence |
| Edges | 3,621 `based_in` (2,849 into San Francisco), 2,436 `founded`, 2,252 `worked_at`, 2,799 `follows`, 1,086 `launched`, 107 `milestone` |
| Layers | wikidata 18.6k, hn 11.9k, x_spider 23.8k, x_ai 8.7k, exa 84, exa_ai 244 |
| Merges | 63 automatic (dedupe); 2 wrong ones split by hand (Ditto, Homebase), which led to S18's rule |
| Review queue | 52 open (`possible_duplicate`, `ambiguous_match`) |
| Wikis | 30 generated (top 15 companies, top 15 people) |

How it was built:

- The X spider fetched all 2,800 accounts davidbooth follows.
- Haiku read their bios. 2,753 bios produced 8.7k claims: roles, companies, locations and founders, each with a quote.
- Exa found fresh SF launches and raises (Ditto, Known, Rocker, Different, …). Haiku turned 142 articles into milestones, and dropped 50 that only mentioned the company in passing.

### Spend so far

| Service | Spent | Notes |
| --- | --- | --- |
| X API | ≈ $29.70 of $40 | 2,800 user reads plus tests. The `X_TOTAL_BUDGET_USD` guard was at $28 overnight. |
| Exa | ≈ $1 | |
| Anthropic | ≈ $0.50 | Parsers $0.22, eval $0.13, 30 wikis about $0.15 |

## Known gaps

- **Person vs company on X.** Accounts are classified by heuristic, so some companies show up as people and the reverse.
- **HN founders** are named by their HN handle until another source names them.
- **Article dates are publish dates.** The same raise reported months later is a separate timeline event.
- **Strict name rules.** Short generic names (Ramp, Meta, Base) don't converge on their own, and borderline cases go to review. Use the Review page.
- **Duplicate pairs left for review:** Figma (2 QIDs), OpenAI (2 QIDs), Cursor vs Anysphere.
- **Scope.** Spider posts and depth-1 expansion phases are off to save X credit. Only davidbooth's following list has been crawled; yoni_michael is configured but not run.
- **Cosign.** Its terms forbid automated access. Use `rebox-worker coverage` on an export you are allowed to use.

## Next

1. Import the overnight bundle into the OrbStack graph (`docs/MERGE.md`).
2. Spend the remaining ~$10 of X credit:
   - yoni_michael's following list (about $5 at $0.01 per user);
   - or a `lookup` of coverage misses (Retool, Rippling, Deel, Supabase, Harvey, Glean founders).
3. Run the coverage checker on a real reference list and enqueue the misses.
4. Work the review queue in the explorer.
