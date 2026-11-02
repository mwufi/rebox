# Coverage checker

How much of a given list of people and companies does Rebox already know? `rebox-worker coverage`
takes a CSV (a portfolio sheet, a conference attendee list, an export from a CRM such as Cosign),
reports which rows Rebox has, how well (facts, edges, sources, timeline) and which are missing,
and can queue the missing ones for the indexers. It measures the graph against a reference list
without scraping anyone.

> **Rebox does not fetch from Cosign** (or any other CRM). The list comes from an export that the
> user is permitted to use, and the checker only reads that file and the Rebox database. Even
> `--enqueue` fetches nothing: it only adds rows to the discovery frontier for the normal,
> budgeted indexers.

## Run it in the compose stack

```sh
docker compose cp list.csv worker:/tmp/list.csv
docker compose exec worker rebox-worker coverage /tmp/list.csv --out /tmp/report.csv
docker compose cp worker:/tmp/report.csv .
```

Add `--enqueue` to queue missing and thin rows, or `--json` to print the summary and every row as
JSON instead of the text summary. Without `--out`, the report is written next to the input as
`<name>.report.csv`. Explorer links in the report use `$EXPLORER_URL` (default
`http://localhost:3000`).

Outside compose: `DATABASE_URL=<app-role URL> uv run rebox-worker coverage list.csv` from
`apps/worker`. The checker connects as `rebox_app` with all public layers visible (the same view as
the pipeline) and runs the matching in a `read only` transaction: it never writes claims.

## Input

A CSV with a header row. Recognised columns (case-insensitive, any subset; every other column is
passed through unchanged to the report):

| column | meaning |
| --- | --- |
| `kind` (or `type`) | `person` or `company`. If absent: `company` when the row has a `domain`/`website` and nothing person-ish (`company`, a `linkedin.com/in/` URL, `first_name`/`last_name`/`title`/`role`), else `person`. |
| `name` (or `first_name` + `last_name`) | display name; `Name (Alias)` searches both, e.g. `Anysphere (Cursor)` |
| `x`, `twitter`, `x_handle` | handle, `@Handle` or an x.com / twitter.com URL |
| `github` | login or github.com URL |
| `domain` | company domain |
| `website`, `url` | a site; for companies its domain is a key (not for x.com, linkedin.com and other platform URLs) |
| `wikidata` | QID or wikidata.org URL |
| `linkedin` | kept and echoed, never matched (Rebox has no LinkedIn key) |
| `company` | for people: prefer the namesake who worked at / founded this company |

Cells may hold several values separated by `,` or `;` (quote the cell).

## Matching

1. **Hard keys** (`matched`): `x_handle`, `github`, `domain`, `wikidata` and `website`, normalized
   exactly as the resolver normalizes them (CONTRACTS §4) and looked up in batches. Merged entities
   resolve to the entity they were merged into. Only entities of the row's kind count. If several
   entities share the key, the oldest is reported (as the resolver would pick) and all of them are
   listed under `candidates`. That is a duplicate worth merging.
2. **Name** (`matched_name`): an equal folded name (lowercase, no punctuation or accents; legal
   forms like `Inc`/`LLC` ignored for companies) scores 1.0, otherwise trigram similarity, and a
   candidate needs at least 0.6. A candidate with a different X handle or GitHub login from the
   row's is dropped: that is a namesake. For a person with a `company`, candidates with a
   `worked_at`/`founded` edge to a company of that name win.
3. **Ambiguous**: two or more candidates within 0.05 of the best (for example two exact namesakes).
   They are listed under `candidates`.
4. **Missing**: nothing above.

## Output

`report.csv` = the input columns + these (prefixed `coverage_` if the input already has a column
with the same name):

| column | |
| --- | --- |
| `status` | `matched`, `matched_name`, `ambiguous`, `missing` |
| `entity_id`, `rebox_name` | the entity and its displayed name |
| `facts` | distinct attribute facts (name, identities, profile, ...) |
| `edges` | distinct edges in either direction, `follows` excluded |
| `sources` | layers with claims about the entity, `;`-separated |
| `timeline` | `y` if it has dated facts (edges with dates, milestones, founding year) |
| `url` | explorer link `$EXPLORER_URL/e/<id>` |
| `candidates` | `id (name, score); ...` for ambiguous rows and duplicate hard-key hits |

The printed summary gives totals and percentages by status and kind, "thin" matches (3 facts or
fewer), how many matches have no timeline, the top sources among matches, and the ambiguous and
missing rows.

## `--enqueue`

Missing rows and thin matches go into `frontier` (CONTRACTS §13) with `status='new'`,
`discovered_by=['coverage:<file stem>']`, score 1:

- with an X handle: `source='x_spider'`, `key='@<handle>'`, `depth=0`. The spider keys accounts by
  X user id, which a list does not have. The `@` prefix keeps the two apart, and depth 0 keeps the
  spider's expansion and post phases from treating the key as an id;
- otherwise: `source='exa'`, `key` = the domain, or the name when there is no domain.

Re-running is idempotent (upsert; `discovered_by` is merged, status never moves backwards). The
command prints how many rows were queued.

## Performance

Hard keys and name candidates are fetched in batches of 2,000 rows (`= any(...)` / `unnest`). On a
copy of the live database (9k entities, 54k claims), 10,000 rows take about 4 s.
