import type { Tx } from './db.js';
import { foldFacts } from './fold.js';

export interface EntityRef {
  id: string;
  kind: string;
  name: string | null;
}

export interface EntityRow {
  id: string;
  kind: string;
  merged_into: string | null;
}

export async function getEntity(tx: Tx, id: string): Promise<EntityRow | null> {
  const [row] = await tx<EntityRow[]>`select id, kind, merged_into from entity where id = ${id}`;
  return row ?? null;
}

/**
 * Follows merged_into to the final (un-merged) entity for each id. Unknown or invisible ids are
 * absent from the result; chains are capped so a corrupt cycle cannot loop.
 */
export async function canonicalIds(tx: Tx, ids: readonly string[]): Promise<Map<string, string>> {
  if (ids.length === 0) return new Map();
  const rows = await tx<{ start: string; id: string }[]>`
    with recursive chain(start, id, depth) as (
      select id, id, 0 from entity where id = any(${[...ids]}::text[])
      union all
      select ch.start, e.merged_into, ch.depth + 1
      from chain ch join entity e on e.id = ch.id
      where e.merged_into is not null and ch.depth < 32
    )
    select distinct on (start) start, id from chain order by start, depth desc`;
  return new Map(rows.map((r) => [r.start, r.id]));
}

/** Ids of every entity whose merged_into chain leads to `id` (excluding `id`). */
export async function mergedFrom(tx: Tx, id: string): Promise<string[]> {
  const rows = await tx<{ id: string }[]>`
    with recursive grp(id) as (
      select id from entity where merged_into = ${id}
      union
      select e.id from entity e join grp g on e.merged_into = g.id
    )
    select id from grp where id <> ${id} order by id`;
  return rows.map((r) => r.id);
}

/** EntityRefs with names from the folded `name` fact (merge-aware). Missing ids are omitted. */
export async function entityRefs(tx: Tx, ids: readonly string[]): Promise<Map<string, EntityRef>> {
  const unique = [...new Set(ids)];
  if (unique.length === 0) return new Map();
  const [rows, names] = await Promise.all([
    tx<{ id: string; kind: string }[]>`select id, kind from entity where id = any(${unique}::text[])`,
    foldFacts(tx, { roots: unique, dir: 'out', predicates: ['name'] }),
  ]);
  const nameOf = new Map<string, string>();
  for (const f of names) {
    if (typeof f.value === 'string') nameOf.set(f.root, f.value);
  }
  return new Map(rows.map((r) => [r.id, { id: r.id, kind: r.kind, name: nameOf.get(r.id) ?? null }]));
}
