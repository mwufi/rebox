# S12 — Hardening for unattended indexers

**Goal:** a long-running indexer never dies because of one bad record, and the two services never disagree about normalization.

**Branch:** `s12-hardening`  **DB prefix:** `s12_`

## Owns
`contracts/normalize-cases.json` (new), `apps/api/src/normalize.ts` + its tests, `apps/worker/rebox_worker/normalize.py` + tests, `apps/worker/rebox_worker/{client,pipeline}.py` (rejection handling) + tests.

## Build
1. **Normalizer parity:** implement CONTRACTS §4 as updated (leading `www.` dropped only when the rest still has a dot) in both services. Create `contracts/normalize-cases.json` (≥ 60 cases across every normalizer: unicode hosts, ports, trailing dots, `www.com`, `www.www.x.com`, IPs, uppercase, t.co, empty/invalid inputs → error) and make both test suites load and pass it.
2. **Partial rejection:** when the writer returns 400 with per-index details for a batch, the worker drops exactly the rejected claims (and claims whose subject/object refs depend on a rejected mint), logs each with its reason, counts them as `rejected` in run stats, writes the rest, and continues. A batch where everything is rejected also continues. Only transport/5xx errors (after retries) and auth errors fail the run. Cursor advances only past successfully written records, as before.
3. **Live fix-up:** after both are in, `rebox-worker replay x_spider` on the architect's live DB must complete; the architect runs that.

## Verification
- api + worker suites green (use your own test DB names: `S2_TEST_DB=s2_s12`, `S1_TEST_DB=s1_s12`).
- A test reproducing the incident: a batch with one non-normalized identity value among 500 claims → 499 written, 1 rejected, run `ok`.
