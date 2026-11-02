/** Response shapes from CONTRACTS §6 (EntityRef, Fact, entity view). */
import type { Tx } from './db.js';
import { canonicalIds, entityRefs, getEntity, mergedFrom, type EntityRef } from './entities.js';
import { foldFacts, type FoldedRow } from './fold.js';

export interface Fact {
  predicate: string;
  value: unknown;
  object: EntityRef | null;
  subject: EntityRef;
  valid_from: string | null;
  valid_to: string | null;
  layer: string;
  source: string;
  source_ref: string;
  updated_at: string;
}

export interface EntityView {
  id: string;
  kind: string;
  name: string | null;
  merged_from: string[];
  facts: Fact[];
  incoming: Fact[];
}

/** Incoming edges on the entity view are capped; the edges endpoint pages through all of them. */
export const INCOMING_CAP = 500;

const iso = (d: Date | null): string | null => (d ? d.toISOString() : null);

function fallbackRef(id: string): EntityRef {
  return { id, kind: id.split(':')[1] ?? 'unknown', name: null };
}

/** Converts folded rows to Facts. Subject/object refs use canonical (merge-followed) ids. */
export async function toFacts(tx: Tx, rows: FoldedRow[], dir: 'out' | 'in'): Promise<Fact[]> {
  const ids = new Set<string>();
  for (const r of rows) {
    ids.add(r.root);
    if (r.other) ids.add(r.other);
  }
  const refs = await entityRefs(tx, [...ids]);
  const ref = (id: string) => refs.get(id) ?? fallbackRef(id);
  return rows.map((r) => {
    const anchor = ref(r.root);
    const other = r.other ? ref(r.other) : null;
    return {
      predicate: r.predicate,
      value: r.value ?? null,
      subject: dir === 'out' ? anchor : other!,
      object: dir === 'out' ? other : anchor,
      valid_from: iso(r.valid_from),
      valid_to: iso(r.valid_to),
      layer: r.layer_id,
      source: r.source,
      source_ref: r.source_ref,
      updated_at: r.updated_at.toISOString(),
    };
  });
}

/** Entity view for an un-merged entity id. */
export async function entityView(tx: Tx, id: string): Promise<EntityView | null> {
  const entity = await getEntity(tx, id);
  if (!entity) return null;
  const [out, inc, from] = await Promise.all([
    foldFacts(tx, { roots: [id], dir: 'out' }),
    foldFacts(tx, { roots: [id], dir: 'in', page: { after: null, limit: INCOMING_CAP } }),
    mergedFrom(tx, id),
  ]);
  const [facts, incoming] = await Promise.all([toFacts(tx, out, 'out'), toFacts(tx, inc, 'in')]);
  const nameFact = out.find((f) => f.predicate === 'name' && typeof f.value === 'string');
  return {
    id: entity.id,
    kind: entity.kind,
    name: nameFact ? (nameFact.value as string) : null,
    merged_from: from,
    facts,
    incoming,
  };
}

export type EntityLookup =
  | { status: 'missing' }
  | { status: 'found'; view: EntityView }
  | { status: 'redirect'; redirect: string; view: EntityView };

/** Resolves an id, following merged_into to the target's view. */
export async function lookupEntity(tx: Tx, id: string): Promise<EntityLookup> {
  const canon = (await canonicalIds(tx, [id])).get(id);
  if (!canon) return { status: 'missing' };
  const view = await entityView(tx, canon);
  if (!view) return { status: 'missing' };
  return canon === id ? { status: 'found', view } : { status: 'redirect', redirect: canon, view };
}
