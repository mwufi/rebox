/**
 * The fold: turn the visible claims about an entity into its effective facts (CONTRACTS §1–2).
 *
 * - Anchor = the requested (root) entity; the claim's anchor side is `subject` for dir=out and
 *   `object_id` for dir=in. Claims on any entity merged (transitively) into the root count as
 *   claims on the root.
 * - Fold key = root + predicate + (canonical other entity for edges | value for `many`
 *   attributes | nothing for `one` attributes).
 * - Winner per key = narrowest layer scope (user > team > firm > public), then newest
 *   updated_at, then a claim made on the root itself over one inherited from a merged-away
 *   entity, then highest id (deterministic). Winners with retract = true are dropped.
 * - Edges whose other side is not visible (RLS on entity) are dropped, so private ids never leak.
 */
import type { Tx } from './db.js';
import { canonCte } from './sqlfrag.js';

export type Dir = 'out' | 'in';

export interface FoldedRow {
  /** The requested entity this fact belongs to (canonical id). */
  root: string;
  claim_id: string;
  predicate: string;
  /** Original subject / object of the winning claim (may be merged-away ids). */
  subject: string;
  object_id: string | null;
  /** Canonical id of the non-anchor side of an edge; null for attributes. */
  other: string | null;
  value: unknown;
  valid_from: Date | null;
  valid_to: Date | null;
  layer_id: string;
  source: string;
  source_ref: string;
  updated_at: Date;
}

export interface FoldOptions {
  roots: readonly string[];
  dir: Dir;
  /** Restrict to these predicates. */
  predicates?: readonly string[] | undefined;
  /** Only edges (always true for dir=in). */
  edgesOnly?: boolean;
  /** Keyset pagination over (predicate, other), for edge listings of a single root. */
  page?: { after: EdgeKey | null; limit: number };
}

export interface EdgeKey {
  predicate: string;
  other: string;
}

export async function foldFacts(tx: Tx, opts: FoldOptions): Promise<FoldedRow[]> {
  if (opts.roots.length === 0) return [];
  const out = opts.dir === 'out';
  const edgesOnly = opts.edgesOnly || !out;
  const anchorCol = out ? tx`c.subject` : tx`c.object_id`;
  const otherCol = out ? tx`c.object_id` : tx`c.subject`;
  const predicateFilter = opts.predicates ? tx`and c.predicate = any(${[...opts.predicates]}::text[])` : tx``;
  const edgeFilter = edgesOnly ? tx`and c.object_id is not null` : tx``;
  const page = opts.page;
  const after = page?.after
    ? tx`and (w.predicate, w.other) > (${page.after.predicate}, ${page.after.other})`
    : tx``;
  const orderAndLimit = page
    ? tx`order by w.predicate, w.other limit ${page.limit}`
    : tx`order by w.root, w.predicate, w.other nulls first, w.value::text`;

  return tx<FoldedRow[]>`
    with recursive ${canonCte(tx)},
    roots(root) as (select distinct unnest(${[...opts.roots]}::text[])),
    grp(root, member) as (
      select root, root from roots
      union all
      select r.root, c.member from roots r join canon c on c.root = r.root
    ),
    cand as (
      select g.root, (g.member = g.root) as direct, c.id, c.predicate, c.subject, c.object_id, c.value, c.valid_from, c.valid_to,
             c.retract, c.layer_id, c.source_ref, c.updated_at, l.scope, l.source,
             case when c.object_id is null then null else coalesce(oc.root, ${otherCol}) end as other,
             case when p.object_kind is not null then coalesce(oc.root, ${otherCol})
                  when p.cardinality = 'many' then c.value::text
                  else '' end as fold_key
      from grp g
      join claim c on ${anchorCol} = g.member
      join layer l on l.id = c.layer_id
      join predicate p on p.id = c.predicate
      left join canon oc on oc.member = ${otherCol}
      where true ${predicateFilter} ${edgeFilter}
    ),
    winners as (
      select distinct on (root, predicate, fold_key) *
      from cand
      order by root, predicate, fold_key, scope desc, updated_at desc, direct desc, id desc
    )
    select w.root, w.id::text as claim_id, w.predicate, w.subject, w.object_id, w.other, w.value,
           w.valid_from, w.valid_to, w.layer_id, w.source, w.source_ref, w.updated_at
    from winners w
    where not w.retract
      and (w.other is null or exists (select 1 from entity oe where oe.id = w.other))
      ${after}
    ${orderAndLimit}`;
}
