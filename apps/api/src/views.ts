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
  /** CONTRACTS §12: the worker that wrote the winning claim (null for pre-0005 claims). */
  worker: string | null;
  evidence: Evidence[] | null;
  /** Distinct (layer, source_ref) rows backing the same fact key: corroboration. */
  support: number;
}

export interface Evidence {
  url: string;
  author?: string;
  quote?: string;
  observed_at?: string;
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
      worker: r.worker_id ?? null,
      evidence: (r.evidence as Evidence[] | null) ?? null,
      support: r.support,
    };
  });
}

/**
 * Facts for specific claim rows (a worker's recent writes), as they were written: not folded, so
 * support is 1 and the claim may since have lost the fold. Claims the viewer cannot see (RLS)
 * and retracted claims are omitted; refs follow merges.
 */
export async function claimFacts(tx: Tx, claimIds: readonly string[]): Promise<Fact[]> {
  if (claimIds.length === 0) return [];
  const rows = await tx<
    (Omit<FoldedRow, 'root' | 'other' | 'support'> & { subject: string; object_id: string | null })[]
  >`
    select c.id::text as claim_id, c.predicate, c.subject, c.object_id, c.value, c.valid_from, c.valid_to,
           c.layer_id, l.source, c.source_ref, c.updated_at, c.worker_id, c.evidence
    from claim c join layer l on l.id = c.layer_id
    where c.id = any(${[...claimIds]}::bigint[]) and not c.retract`;
  const order = new Map(claimIds.map((id, i) => [id, i]));
  rows.sort((a, b) => order.get(a.claim_id)! - order.get(b.claim_id)!);
  const ids = rows.flatMap((r) => (r.object_id ? [r.subject, r.object_id] : [r.subject]));
  const canon = await canonicalIds(tx, ids);
  const folded: FoldedRow[] = rows.flatMap((r) => {
    const root = canon.get(r.subject);
    if (!root) return [];
    const other = r.object_id ? canon.get(r.object_id) : null;
    if (r.object_id && !other) return []; // the other side is not visible
    return [{ ...r, root, other: other ?? null, support: 1 }];
  });
  return toFacts(tx, folded, 'out');
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
