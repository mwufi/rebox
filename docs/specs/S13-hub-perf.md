# S13 — Hub entities under 50 ms

**Goal:** entity view and edges for hub entities (e.g. San Francisco with 2,277+ incoming edges, soon larger with the X `follows` graph) stay under 50 ms p95, without changing results.

**Branch:** `s13-hub-perf`  **DB prefix:** `s13_`

## Owns
`apps/api/src/fold.ts`, `apps/api/src/views.ts`, read routes that call them, related tests and bench scripts; a new migration `db/migrations/0008_*.sql` only if an index is needed (explain why).

## Build
- The entity view must not fold every incoming claim before limiting. Return incoming edges **paged and grouped**: per predicate, the first N (e.g. 12) folded edges plus a total count, with a cursor per predicate via the existing `/v0/entities/:id/edges`. Keep the response contract compatible: `incoming` stays an array of Facts (now limited), and add `incoming_counts: {predicate: n}` (document in your report as a contract addition).
- `/v0/entities/:id/edges` must page before folding: select candidate (subject, predicate, object) keys in index order with a limit, then fold only those keys.
- Outgoing facts for hubs (people following 3,000 accounts via `follows`) need the same treatment: cap `follows` and any `many` edge predicate at N in the view, with counts.
- Support counts and trust tie-breaks must stay identical for the rows returned (add equivalence tests comparing old vs new fold output on small entities).
- Update the explorer only where needed to show "+N more" using the counts (the entity page already links to the graph/edges views).

## Verification
- Bench on a copy of `rebox_live` (`createdb -T rebox_live s13_live`; it has the X follows graph, so davidbooth is an outgoing hub and San Francisco an incoming hub): p50/p95 before/after for SF view, SF edges, davidbooth view, Brex, a random person; target p95 < 50 ms for all.
- api + web suites green on your own DB names (`S1_TEST_DB=s1_s13`).
