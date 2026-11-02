import type { Tx } from './db.js';
import { foldFacts } from './fold.js';

export interface EntityRef {
  id: string;
  kind: string;
  name: string | null;
  /** S10, optional: the entity's folded `avatar` URL, only when it has one. */
  avatar?: string;
  /** S10, optional: set when the name is only a handle (equals its hn_user / x_handle). */
  handle?: 'hn' | 'x';
}

/** Predicates entityRefs folds: the name plus what the explorer needs to draw a ref. */
const REF_PREDICATES = ['name', 'avatar', 'hn_user', 'x_handle'];
const isHttpUrl = (v: unknown): v is string => typeof v === 'string' && /^https?:\/\/\S+$/i.test(v);

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

/**
 * EntityRefs with names from the folded `name` fact (merge-aware), plus `avatar` and the `handle`
 * hint when they apply. Missing ids are omitted.
 */
export async function entityRefs(tx: Tx, ids: readonly string[]): Promise<Map<string, EntityRef>> {
  const unique = [...new Set(ids)];
  if (unique.length === 0) return new Map();
  const [rows, folded] = await Promise.all([
    tx<{ id: string; kind: string }[]>`select id, kind from entity where id = any(${unique}::text[])`,
    foldFacts(tx, { roots: unique, dir: 'out', predicates: REF_PREDICATES }),
  ]);
  const nameOf = new Map<string, string>();
  const avatarOf = new Map<string, string>();
  const handles = new Map<string, { hn: Set<string>; x: Set<string> }>();
  for (const f of folded) {
    if (f.predicate === 'name' && typeof f.value === 'string') nameOf.set(f.root, f.value);
    else if (f.predicate === 'avatar' && isHttpUrl(f.value)) avatarOf.set(f.root, f.value);
    else if ((f.predicate === 'hn_user' || f.predicate === 'x_handle') && typeof f.value === 'string') {
      const h = handles.get(f.root) ?? { hn: new Set<string>(), x: new Set<string>() };
      (f.predicate === 'hn_user' ? h.hn : h.x).add(f.value.toLowerCase());
      handles.set(f.root, h);
    }
  }
  return new Map(
    rows.map((r) => {
      const ref: EntityRef = { id: r.id, kind: r.kind, name: nameOf.get(r.id) ?? null };
      const avatar = avatarOf.get(r.id);
      if (avatar) ref.avatar = avatar;
      const h = handles.get(r.id);
      const n = ref.name?.replace(/^@/, '').toLowerCase();
      if (h && n) {
        if (h.hn.has(n)) ref.handle = 'hn';
        else if (h.x.has(n)) ref.handle = 'x';
      }
      return [r.id, ref];
    }),
  );
}
