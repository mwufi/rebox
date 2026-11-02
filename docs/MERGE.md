# Merging graphs between Rebox instances

`rebox-worker export` writes whole layers of one instance to a bundle file; `rebox-worker import`
merges a bundle into another instance. Nothing on the target is wiped or overwritten wholesale:
the import adds the bundle's claims, keeps their workers, runs and evidence, and sends every entity
through the **target's resolver**, so a person the target already knows under another id (say,
minted from their GitHub login) gets the new facts instead of a duplicate.

How it decides, per entity in the bundle:

1. **Hard keys**: any identity value the source knows for it (`x_handle`, `x_id`, `domain`,
   `github`, `wikidata`, ...; from claims in any layer and from the source's resolution log)
   matches an identity claim on the target → that entity. Several target entities match → the
   oldest, plus an `ambiguous_match` review item.
2. **Origin id**: the source's id already exists on the target (both sides derived it the same
   way, e.g. a Wikidata place) → that entity.
3. **Mint**: a new entity, with the id the target's resolver derives (the source's id when there
   is no minting key, e.g. a company a parser only knows by name).

Two kinds of hint are *not* used for matching: `website` values (a person's website is often
their employer's; pass `--match-website` to use them) and any value two different entities in
the bundle share. The claims themselves are always imported.

Re-running an import is safe: claims already on the target are skipped, so a second run writes
nothing at all (no claims, runs or log rows).

## Tomorrow: overnight layers → Zen's OrbStack stack

Assumes the stack from `docker-compose.yml` (services `db`, `api`, `worker`), running `main` with
this change.

**0. On the overnight instance, export** (here: the `rebox_live` database on port 54329):

```sh
cd apps/worker
DATABASE_URL=postgres://rebox_app:rebox_app@localhost:54329/rebox_live \
  uv run rebox-worker export --layers public/x_spider,public/exa --out ~/overnight.rebox.jsonl.gz
```

It prints claims per layer, the entity count and the workers. Add layers built later
(`public/x_ai`, ...) to `--layers`; the export reads one consistent snapshot, so it is fine to
run while spiders are writing. Copy the file to Zen's machine.

**1. Update and back up Zen's stack**

```sh
git pull                                   # main with S11
docker compose up -d --build               # rebuilds api/worker images; `migrate` applies 0005 if missing
docker compose exec -T db pg_dump -U postgres rebox | gzip > ~/rebox-before-merge.sql.gz
```

**2. Copy the bundle into the worker container**

```sh
docker compose cp ~/overnight.rebox.jsonl.gz worker:/tmp/overnight.rebox.jsonl.gz
```

**3. Dry run** (reads only; writes nothing anywhere):

```sh
docker compose exec worker rebox-worker import /tmp/overnight.rebox.jsonl.gz --dry-run
```

Read the report:

- `workers`: each bundle worker is `register (new)` or `exists`. A `conflict` (the same worker
  id on Zen's side with another source) stops the import; nothing is written.
- `claims by layer`: `new` / `changed` / `unchanged` / `rejected`.
- `entities` per kind: `minted` (new on Zen's side), `matched_same_id` (Zen already has it under
  the same id, e.g. Wikidata companies), `matched_other_id` (merged onto one of Zen's entities
  with a different id — the examples show origin id → Zen's id and the key that matched; check
  a few), `review` (ambiguous; a review item is opened).
- `conflicts`: one-valued attributes (`name`, `headline`, `description`, ...) where Zen's
  current value differs. `incoming wins` means the fold will show the imported value: X spider
  claims have trust 0.8, Wikidata/HN claims written before workers existed count as 0.5, and on a
  tie (e.g. GitHub connector 0.8) the newer claim wins. Admins can lower a worker's trust later
  (`PATCH /v0/admin/workers/:id {"trust": ...}`) to change that.

`--json` prints the same report as JSON; `--examples 50` shows more examples;
`--layers public/exa` imports a subset.

**4. Import**

```sh
docker compose exec worker rebox-worker import /tmp/overnight.rebox.jsonl.gz
```

Each bundle worker writes in its own run (kind `replay`; stats include `written`, `minted`,
`matched`, `rejected` and the bundle it came from). About 25 s for the 22k claims of
`public/x_spider` + `public/exa`.

**5. Check**

```sh
docker compose exec worker rebox-worker import /tmp/overnight.rebox.jsonl.gz --dry-run   # all "unchanged"
docker compose exec worker rebox-worker workers list
curl -s localhost:3000/v0/stats
```

The workers page in the explorer shows the imported workers and their `replay` runs.

## Notes

- **Rejected claims.** The resolver drops claims the target's vocabulary rejects, and if the
  writer rejects specific claims in a batch (e.g. a URL its normalizer writes differently), only
  those are dropped and listed under `rejected`; the rest of the batch is written.
- **Undo.** Restore the backup from step 1. (Every imported claim carries the `run_id` of its
  import run, so they can also be found with `select ... from claim where run_id in (...)`.)
- **Workers without an id.** Claims written before migration 0005 have no worker; they import as
  the target's `<source>-connector` worker. External workers cannot be registered with the
  ingest token: create them on the target first, or leave their layers out.
- **Private layers** are not exported (the export reads public layers only).
