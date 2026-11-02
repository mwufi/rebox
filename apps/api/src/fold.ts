/**
 * The fold: turn the visible claims about an entity into its effective facts (CONTRACTS §1–2).
 *
 * - Anchor = the requested (root) entity; the claim's anchor side is `subject` for dir=out and
 *   `object_id` for dir=in. Claims on any entity merged (transitively) into the root count as
 *   claims on the root.
 * - Fold key = root + predicate + (canonical other entity for edges | value for `many`
 *   attributes | nothing for `one` attributes).
 * - Winner per key = narrowest layer scope (user > team > firm > public), then the writing
 *   worker's trust (CONTRACTS §12; claims written before workers existed count as 0.5), then
 *   newest updated_at, then a claim made on the root itself over one inherited from a
 *   merged-away entity, then highest id (deterministic). Winners with retract = true are dropped.
 * - support = distinct (layer, source_ref) among the non-retracted candidates for the same key
 *   (and, for `one` attributes, the same value as the winner): how many sources corroborate the
 *   fact. It is computed from the candidate rows the fold already reads, so it adds no scan.
 * - Edges whose other side is not visible (RLS on entity) are dropped, so private ids never leak.
 *
 * Hubs (S13): `foldFacts` folds every candidate claim of a root before any limit applies, which is
 * linear in the root's degree (San Francisco: 2,277 incoming `based_in`; an X spider seed: 2,000+
 * outgoing `follows`). `foldEdgePage` and `foldEdgeGroups` page first:
 *   1. `keys`: one pass over the root's edge claims (claim_subject_idx / claim_object_idx) groups
 *      them by fold key (predicate + canonical other) and keeps each key's claim ids; no fold.
 *   2. A key is alive when `foldFacts` would return a row for it: the other side is visible and the
 *      winner is not a retraction. The winner is only looked up for keys with a retracted claim.
 *   3. The page's keys are picked, and the fold runs on exactly their claim ids (primary key).
 * Rows, support and tie-breaks are therefore those of `foldFacts` for the keys returned, by
 * construction (same candidate set, same `winnerOrder`, same support window); the equivalence is
 * tested in test/fold-paged.test.ts.
 */
import type { Frag, Tx } from './db.js';
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
  worker_id: string | null;
  evidence: unknown[] | null;
  support: number;
}

/** Trust assumed for claims with no worker (written before migration 0005). */
export const UNATTRIBUTED_TRUST = 0.5;

export interface FoldOptions {
  roots: readonly string[];
  dir: Dir;
  /** Restrict to these predicates. */
  predicates?: readonly string[] | undefined;
  /** Only edges (always true for dir=in). */
  edgesOnly?: boolean;
  /** Only attributes (dir=out); the entity view folds edges separately with `foldEdgeGroups`. */
  attributesOnly?: boolean;
  /** Keyset pagination over (predicate, other), for edge listings of a single root. */
  page?: { after: EdgeKey | null; limit: number };
}

export interface EdgeKey {
  predicate: string;
  other: string;
}

/**
 * Fold winner order within a key (CONTRACTS §2, §12): narrowest scope, then worker trust, then
 * newest write, then a claim made on the root itself over one inherited through a merge, then
 * highest id. Every fold path sorts by this, over the columns from `rankCols`.
 */
const winnerOrder = (tx: Tx) => tx`scope desc, trust desc, updated_at desc, direct desc, id desc`;

/** The columns `winnerOrder` sorts by, from `claim c`, `layer l`, `worker wk` and a `direct` flag. */
const rankCols = (tx: Tx, direct: Frag) =>
  tx`l.scope, coalesce(wk.trust, ${UNATTRIBUTED_TRUST}::real) as trust, c.updated_at, (${direct}) as direct, c.id`;

/**
 * `with recursive` prelude: canon (merge map), roots, and grp(root, member) = each root's merge
 * group. A single root is a plain parameter rather than `unnest($1)`, so generic plans estimate
 * one root as custom plans do (see `preferGenericPlans`).
 */
function prelude(tx: Tx, roots: readonly string[]) {
  const rootRows =
    roots.length === 1 ? tx`select ${roots[0]!}::text` : tx`select distinct unnest(${[...roots]}::text[])`;
  return tx`
    ${canonCte(tx)},
    roots(root) as (${rootRows}),
    grp(root, member) as (
      select root, root from roots
      union all
      select r.root, c.member from roots r join canon c on c.root = r.root
    )`;
}

const anchorOf = (tx: Tx, dir: Dir) => (dir === 'out' ? tx`c.subject` : tx`c.object_id`);
const otherOf = (tx: Tx, dir: Dir) => (dir === 'out' ? tx`c.object_id` : tx`c.subject`);

/**
 * CTE body for `cand`: candidate claim rows with everything the fold needs. `from` must bind
 * `claim c`; `root` and `direct` are expressions over it.
 */
function candidates(tx: Tx, dir: Dir, from: Frag, root: Frag, direct: Frag) {
  const otherCol = otherOf(tx, dir);
  return tx`
    select ${root} as root, ${rankCols(tx, direct)}, c.predicate, c.subject, c.object_id, c.value, c.valid_from, c.valid_to,
           c.retract, c.layer_id, c.source_ref, c.worker_id, c.evidence, l.source,
           case when c.object_id is null then null else coalesce(oc.root, ${otherCol}) end as other,
           case when p.object_kind is not null then coalesce(oc.root, ${otherCol})
                when p.cardinality = 'many' then c.value::text
                else '' end as fold_key,
           -- For one-cardinality attributes, only rows with the same value corroborate the winner.
           case when p.object_kind is null and p.cardinality = 'one' then c.value::text else '' end as support_key
    from ${from}
    join layer l on l.id = c.layer_id
    join predicate p on p.id = c.predicate
    left join worker wk on wk.id = c.worker_id
    left join canon oc on oc.member = ${otherCol}`;
}

/**
 * CTE bodies `ranked`, `counted`, `winners` over `cand`, and the final select of live winners.
 * Support in one pass with window functions (a join back to an aggregate of cand gets a
 * nested-loop plan, since cand is estimated at one row, and turns quadratic on hubs).
 * dup = 1 marks the first live row per (layer, source_ref) within a support group.
 *
 * Visibility probes on entity are written `exists (... offset 0)`: the offset stops Postgres from
 * turning the EXISTS into a hashed subplan, which reads every entity once (a seq scan with the RLS
 * filter on each row) instead of probing entity_pkey per row. Generic plans, which estimate many
 * outer rows, otherwise pick the hashed form.
 */
function foldTail(tx: Tx, extraCols: Frag, extraJoin: Frag, filter: Frag, orderAndLimit: Frag) {
  return tx`
    ranked as (
      select *, row_number() over (partition by root, predicate, fold_key, support_key, layer_id, source_ref, retract
                                   order by id) as dup
      from cand
    ),
    counted as (
      select *, count(*) filter (where not retract and dup = 1)
                  over (partition by root, predicate, fold_key, support_key)::int as support
      from ranked
    ),
    winners as (
      select distinct on (root, predicate, fold_key) *
      from counted
      order by root, predicate, fold_key, ${winnerOrder(tx)}
    )
    select w.root, w.id::text as claim_id, w.predicate, w.subject, w.object_id, w.other, w.value,
           w.valid_from, w.valid_to, w.layer_id, w.source, w.source_ref, w.updated_at,
           w.worker_id, w.evidence, w.support ${extraCols}
    from winners w ${extraJoin}
    where not w.retract
      and (w.other is null or exists (select 1 from entity oe where oe.id = w.other offset 0))
      ${filter}
    ${orderAndLimit}`;
}

/** Folds every visible claim of `roots` in one direction (see the module comment). */
export async function foldFacts(tx: Tx, opts: FoldOptions): Promise<FoldedRow[]> {
  if (opts.roots.length === 0) return [];
  const out = opts.dir === 'out';
  const edgesOnly = opts.edgesOnly || !out;
  if (edgesOnly && opts.attributesOnly) return [];
  const anchorCol = anchorOf(tx, opts.dir);
  const predicateFilter = opts.predicates ? tx`and c.predicate = any(${[...opts.predicates]}::text[])` : tx``;
  const kindFilter = edgesOnly ? tx`and c.object_id is not null` : opts.attributesOnly ? tx`and c.object_id is null` : tx``;
  const page = opts.page;
  const after = page?.after
    ? tx`and (w.predicate, w.other) > (${page.after.predicate}, ${page.after.other})`
    : tx``;
  const orderAndLimit = page
    ? tx`order by w.predicate, w.other limit ${page.limit}`
    : tx`order by w.root, w.predicate, w.other nulls first, w.value::text`;

  return tx<FoldedRow[]>`
    with recursive ${prelude(tx, opts.roots)},
    cand as (
      ${candidates(
        tx,
        opts.dir,
        tx`grp g join claim c on ${anchorCol} = g.member ${predicateFilter} ${kindFilter}`,
        tx`g.root`,
        tx`g.member = g.root`,
      )}
    ),
    ${foldTail(tx, tx``, tx``, after, orderAndLimit)}`;
}

/**
 * CTE `keys(predicate, other, any_retract, ids)`: one row per edge fold key of a single root, with
 * the ids of every visible claim behind it (exactly the fold's candidate set for that key). One
 * pass over claim_subject_idx / claim_object_idx; nothing is folded.
 */
function edgeKeys(tx: Tx, root: string, dir: Dir, predicates: readonly string[] | undefined) {
  const anchorCol = anchorOf(tx, dir);
  const otherCol = otherOf(tx, dir);
  const predicateFilter = predicates ? tx`and c.predicate = any(${[...predicates]}::text[])` : tx``;
  return tx`
    ${prelude(tx, [root])},
    keys as (
      select c.predicate, coalesce(oc.root, ${otherCol}) as other, bool_or(c.retract) as any_retract,
             array_agg(c.id) as ids
      from grp g
      join claim c on ${anchorCol} = g.member
      left join canon oc on oc.member = ${otherCol}
      where c.object_id is not null ${predicateFilter}
      group by 1, 2
    ),
    -- Entity RLS is home_layer = any(rebox_visible()), and home_layer is a not-null reference to
    -- layer (which has no RLS). So when every layer is visible, so is every entity, and the
    -- per-key visibility probe can be skipped (one pass over a handful of layer rows instead).
    vis(all_entities) as (select not exists (select 1 from layer where not (id = any(rebox_visible()))))`;
}

/**
 * Condition on a key row `k`: true when `foldFacts` would return a row for it, i.e. the other side
 * is visible (RLS on entity; skipped when `vis` proves every entity visible) and the key's winner
 * is not a retraction. The winner is only looked
 * up (by claim id) for keys that have a retracted claim at all, which is rare.
 */
function keyAlive(tx: Tx, root: string, dir: Dir) {
  const anchorCol = anchorOf(tx, dir);
  return tx`
    ((select v.all_entities from vis v) or exists (select 1 from entity e where e.id = k.other offset 0))
    and (not k.any_retract or not (
      select x.retract from (
        select c.retract, ${rankCols(tx, tx`${anchorCol} = ${root}::text`)}
        from claim c
        join layer l on l.id = c.layer_id
        left join worker wk on wk.id = c.worker_id
        where c.id = any(k.ids)
      ) x
      order by ${winnerOrder(tx)}
      limit 1))`;
}

/** CTE body `cand` for the keys in `picked(predicate, other, ids)`: their claims, by primary key. */
function pickedCandidates(tx: Tx, root: string, dir: Dir) {
  const anchorCol = anchorOf(tx, dir);
  return candidates(
    tx,
    dir,
    tx`picked pk cross join lateral unnest(pk.ids) as pc(id) join claim c on c.id = pc.id`,
    tx`${root}::text`,
    tx`${anchorCol} = ${root}::text`,
  );
}

/**
 * Makes the rest of the transaction use generic plans for its prepared statements. The fold
 * statements take 4–15 ms to plan and well under that to run on a typical entity; in `auto` mode
 * Postgres re-plans them on every call because generic-plan estimates for these CTE-driven
 * queries look worse than custom ones, although both are the same index-driven plan. With
 * generic plans each pooled connection plans a statement once.
 */
export async function preferGenericPlans(tx: Tx): Promise<void> {
  await tx`select set_config('plan_cache_mode', 'force_generic_plan', true)`;
}

export interface EdgePageOptions {
  /** Canonical (un-merged) root id. */
  root: string;
  dir: Dir;
  predicates?: readonly string[] | undefined;
  after: EdgeKey | null;
  limit: number;
}

/**
 * One page of a root's folded edges in (predicate, other) order: the same rows as
 * `foldFacts({roots: [root], dir, edgesOnly: true, predicates, page: {after, limit}})`, but the
 * page's keys are picked before anything is folded, so a hub costs one pass over its claim index
 * entries plus a fold of `limit` keys.
 */
export async function foldEdgePage(tx: Tx, opts: EdgePageOptions): Promise<FoldedRow[]> {
  const after = opts.after ? tx`where (k.predicate, k.other) > (${opts.after.predicate}, ${opts.after.other})` : tx``;
  // Keys are sorted first and checked in that order until the page is full, so a hub page probes
  // about `limit` keys rather than all of them. `offset 0` keeps the alive check from being pushed
  // below the sort; the outer `order by` is then satisfied by the sorted subquery (no re-sort).
  return tx<FoldedRow[]>`
    with recursive ${edgeKeys(tx, opts.root, opts.dir, opts.predicates)},
    picked as (
      select k.predicate, k.other, k.ids
      from (select * from keys k ${after} order by k.predicate, k.other offset 0) k
      where ${keyAlive(tx, opts.root, opts.dir)}
      order by k.predicate, k.other
      limit ${opts.limit}
    ),
    cand as (${pickedCandidates(tx, opts.root, opts.dir)}),
    ${foldTail(tx, tx``, tx``, tx``, tx`order by w.predicate, w.other`)}`;
}

export interface EdgeGroups {
  /** At most `perPredicate` folded edges per predicate, in (predicate, other) order. */
  rows: FoldedRow[];
  /** Folded edge count per predicate (every predicate with at least one edge). */
  counts: Record<string, number>;
}

/**
 * The first `perPredicate` folded edges of each predicate (in `other` order, so the edges endpoint
 * continues where they stop) plus each predicate's total, folding only the returned keys.
 */
export async function foldEdgeGroups(
  tx: Tx,
  opts: { root: string; dir: Dir; perPredicate: number; predicates?: readonly string[] | undefined },
): Promise<EdgeGroups> {
  const rows = await tx<(FoldedRow & { total: number })[]>`
    with recursive ${edgeKeys(tx, opts.root, opts.dir, opts.predicates)},
    numbered as (
      select k.predicate, k.other, k.ids,
             row_number() over (partition by k.predicate order by k.other) as rn,
             count(*) over (partition by k.predicate)::int as total
      from keys k
      where ${keyAlive(tx, opts.root, opts.dir)}
    ),
    picked as (select predicate, other, ids, total from numbered where rn <= ${opts.perPredicate}),
    cand as (${pickedCandidates(tx, opts.root, opts.dir)}),
    ${foldTail(
      tx,
      tx`, (select pt.total from picked pt where pt.predicate = w.predicate limit 1) as total`,
      tx``,
      tx``,
      tx`order by w.predicate, w.other`,
    )}`;
  const counts: Record<string, number> = {};
  const out: FoldedRow[] = rows.map(({ total, ...r }) => {
    counts[r.predicate] = total;
    return r;
  });
  return { rows: out, counts };
}
