/**
 * Query compiler (CONTRACTS §6 "Query"). Compiles a filter list into ONE SQL statement:
 *   entity of kind `find`, un-merged, AND of `exists` subqueries over `claim`.
 * Filters match only *effective* facts: each subquery requires the matched claim to be the fold
 * winner for its key (no visible claim in a narrower scope, or newer in the same scope, for the
 * same subject group + predicate + key) and not retracted. Claims on merged-away entities count
 * for their merge target. Each filter also yields a `reasons` string for result rows.
 */
import { z } from 'zod';
import type { Frag, Tx } from './db.js';
import { decodeCursor, encodeCursor, isIdCursor } from './cursor.js';
import { isIsoDate, isoToMs } from './dates.js';
import { entityRefs } from './entities.js';
import { validation, type ErrorDetail } from './errors.js';
import { canonCte, likeEscape, privateVisibleLayers } from './sqlfrag.js';
import { isEdge, type Predicate, type Vocab } from './vocab.js';

export const MAX_FILTERS = 10;
export const MAX_LIMIT = 200;

const scalar = z.union([z.string(), z.number(), z.boolean()]);
const isoDate = z.string().refine(isIsoDate, 'must be an ISO date or timestamp');

const nameFilter = z.object({ name: z.string().trim().min(1).max(200) }).strict();
const edgeFilter = z
  .object({
    edge: z
      .object({
        predicate: z.string().min(1),
        dir: z.enum(['out', 'in']).default('out'),
        to: z.string().min(1).nullish(),
        during: z.object({ from: isoDate.nullish(), to: isoDate.nullish() }).strict().nullish(),
      })
      .strict(),
  })
  .strict();
const attrFilter = z
  .object({
    attr: z
      .object({
        predicate: z.string().min(1),
        eq: scalar.optional(),
        gte: z.union([z.string(), z.number()]).optional(),
        lte: z.union([z.string(), z.number()]).optional(),
        contains: z.string().min(1).optional(),
      })
      .strict()
      .refine((a) => a.eq !== undefined || a.gte !== undefined || a.lte !== undefined || a.contains !== undefined, {
        message: 'attr needs at least one of eq, gte, lte, contains',
      }),
  })
  .strict();
const filterSchema = z.union([nameFilter, edgeFilter, attrFilter]);

export const querySchema = z
  .object({
    find: z.string().min(1),
    where: z.array(z.unknown()).max(MAX_FILTERS).default([]),
    limit: z.number().int().min(1).max(MAX_LIMIT).default(50),
    cursor: z.string().nullish(),
  })
  .strict();

type NameFilter = z.infer<typeof nameFilter>;
type EdgeFilter = z.infer<typeof edgeFilter>['edge'];
type AttrFilter = z.infer<typeof attrFilter>['attr'];

export type CompiledFilter =
  | { type: 'name'; text: string }
  | { type: 'edge'; p: Predicate; f: EdgeFilter }
  | { type: 'attr'; p: Predicate; f: AttrFilter };

export interface QueryPlan {
  find: string;
  filters: CompiledFilter[];
  limit: number;
  after: string | null;
}

export interface QueryResult {
  results: { id: string; kind: string; name: string | null; reasons: string[] }[];
  next: string | null;
}

function zodMessage(err: z.ZodError): string {
  return err.issues.map((i) => (i.path.length ? `${i.path.join('.')}: ${i.message}` : i.message)).join('; ');
}

const isNumeric = (p: Predicate) => p.meta.value_type === 'int' || p.meta.value_type === 'number';

/** Validates the request against the vocabulary and returns a plan, or throws 400. */
export function planQuery(body: unknown, vocab: Vocab): QueryPlan {
  const parsed = querySchema.safeParse(body);
  if (!parsed.success) throw validation([{ index: -1, message: zodMessage(parsed.error) }]);
  const q = parsed.data;
  const details: ErrorDetail[] = [];
  if (!vocab.kinds.has(q.find)) details.push({ index: -1, message: `unknown kind ${JSON.stringify(q.find)}` });

  const filters: CompiledFilter[] = [];
  q.where.forEach((raw, index) => {
    const r = filterSchema.safeParse(raw);
    if (!r.success) {
      details.push({ index, message: `filter must be one of {name}, {edge}, {attr}: ${zodMessage(r.error)}` });
      return;
    }
    const f = r.data;
    if ('name' in f) {
      filters.push({ type: 'name', text: (f as NameFilter).name });
      return;
    }
    const spec = 'edge' in f ? f.edge : f.attr;
    const p = vocab.predicates.get(spec.predicate);
    const fail = (message: string) => details.push({ index, message });
    if (!p) return fail(`unknown predicate ${JSON.stringify(spec.predicate)}`);
    if (!p.meta.filterable) return fail(`predicate ${p.id} is not filterable`);
    if ('edge' in f) {
      if (!isEdge(p)) return fail(`${p.id} is an attribute; use {attr}`);
      const kindOk = f.edge.dir === 'out' ? p.subject_kinds.includes(q.find) : p.object_kind === q.find;
      if (!kindOk) return fail(`${p.id} (dir ${f.edge.dir}) cannot apply to kind ${q.find}`);
      const d = f.edge.during;
      if (d?.from && d.to && isoToMs(d.from) > isoToMs(d.to)) return fail('during.from must not be after during.to');
      filters.push({ type: 'edge', p, f: f.edge });
    } else {
      if (isEdge(p)) return fail(`${p.id} is an edge; use {edge}`);
      if (!p.subject_kinds.includes(q.find)) return fail(`${p.id} does not apply to kind ${q.find}`);
      const a = f.attr;
      if (isNumeric(p)) {
        for (const op of ['eq', 'gte', 'lte'] as const) {
          if (a[op] !== undefined && typeof a[op] !== 'number') return fail(`${p.id}.${op} must be a number`);
        }
        if (a.contains !== undefined) return fail(`contains is only for text predicates`);
      } else if (p.meta.value_type === 'bool') {
        if (a.gte !== undefined || a.lte !== undefined || a.contains !== undefined) return fail(`${p.id} supports eq only`);
        if (a.eq !== undefined && typeof a.eq !== 'boolean') return fail(`${p.id}.eq must be a boolean`);
      } else {
        for (const op of ['eq', 'gte', 'lte'] as const) {
          if (a[op] !== undefined && typeof a[op] !== 'string') return fail(`${p.id}.${op} must be a string`);
        }
      }
      filters.push({ type: 'attr', p, f: a });
    }
  });
  if (details.length) throw validation(details);
  const after = decodeCursor(q.cursor, isIdCursor)?.id ?? null;
  return { find: q.find, filters, limit: q.limit, after };
}

// ── SQL generation ──────────────────────────────────────────────────────────────────────────

/** Canonical (merge-followed) id of an entity id expression. Needs the `canon` CTE. */
const canonOf = (tx: Tx, expr: Frag) =>
  tx`coalesce((select cn.root from canon cn where cn.member = ${expr}), ${expr})`;

/** The id plus every entity merged (transitively) into it, as a text[] expression. */
const groupOf = (tx: Tx, canonExpr: Frag) =>
  tx`array(select ${canonExpr} union all select cn.member from canon cn where cn.root = ${canonExpr})`;

/** Date-only upper bounds cover the whole day. */
function upperBound(to: string): { op: 'lt' | 'le'; at: string } {
  if (to.length !== 10) return { op: 'le', at: to };
  const d = new Date(`${to}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return { op: 'lt', at: d.toISOString() };
}

/**
 * `from ... where ...` for one filter, anchored on `anchorExpr` (an un-merged entity id).
 * Reads from anchoredClaims, so claims on merged-away entities count for their target and the
 * anchor test is a plain equality (index-friendly). The anti-join on c2 is the fold expressed
 * per row: c must be the winner for its fold key, and not retracted.
 */
function filterSource(tx: Tx, cf: CompiledFilter, anchorExpr: Frag) {
  const dir = cf.type === 'edge' ? cf.f.dir : 'out';
  const predicate = cf.type === 'name' ? 'name' : cf.p.id;

  let sameKey = tx``;
  if (cf.type === 'edge') sameKey = tx`and ${canonOf(tx, tx`c2.other`)} = ${canonOf(tx, tx`c.other`)}`;
  else if (cf.type === 'attr' && cf.p.cardinality === 'many') sameKey = tx`and c2.value = c.value`;

  return tx`
    from ${anchoredClaims(tx, dir)} c join layer l on l.id = c.layer_id
    where c.anchor = ${anchorExpr} and c.predicate = ${predicate} and not c.retract
      and ${matchCondition(tx, cf)}
      and not exists (
        select 1 from ${anchoredClaims(tx, dir)} c2 join layer l2 on l2.id = c2.layer_id
        where c2.anchor = c.anchor and c2.predicate = c.predicate ${sameKey}
          and (l2.scope, c2.updated_at, c2.direct, c2.id) > (l.scope, c.updated_at, c.direct, c.id)
      )`;
}

/**
 * Visible claims with `anchor` = the entity the claim is about in direction `dir` (subject for
 * out, object for in) and `other` = the raw other side. Claims on merged-away entities appear a
 * second time anchored on their canonical target (`direct` = 0; the fold prefers direct claims on
 * ties, matching fold.ts). Needs the `canon` CTE.
 */
export function anchoredClaims(tx: Tx, dir: 'out' | 'in') {
  const cols = tx`c.id, c.layer_id, c.predicate, c.value, c.value_text, c.valid_from, c.valid_to, c.retract, c.updated_at`;
  return dir === 'out'
    ? tx`(select ${cols}, c.subject as anchor, c.object_id as other, 1 as direct from claim c
          union all
          select ${cols}, cn.root as anchor, c.object_id as other, 0 as direct
          from canon cn join claim c on c.subject = cn.member)`
    : tx`(select ${cols}, c.object_id as anchor, c.subject as other, 1 as direct from claim c
          union all
          select ${cols}, cn.root as anchor, c.subject as other, 0 as direct
          from canon cn join claim c on c.object_id = cn.member)`;
}

/** The filter's own predicate on the claim `c` (value / other side / validity). */
function matchCondition(tx: Tx, cf: CompiledFilter) {
  if (cf.type === 'name') {
    // CONTRACTS §2a: compare values through value_text, never value #>> '{}'.
    return tx`c.value_text ilike ${likePattern(cf.text)}`;
  }
  if (cf.type === 'edge') {
    const { to, during } = cf.f;
    // The other side must be visible (RLS on entity), as in the fold.
    const parts = [tx`c.other is not null`, tx`exists (select 1 from entity oe where oe.id = ${canonOf(tx, tx`c.other`)})`];
    if (to) {
      const toCanon = canonOf(tx, tx`${to}::text`);
      // `c.other = any(group)` lets the planner use claim_object_idx / claim_subject_idx.
      parts.push(tx`c.other = any(${groupOf(tx, toCanon)})`);
    }
    if (during?.to) {
      const ub = upperBound(during.to);
      parts.push(
        ub.op === 'lt'
          ? tx`(c.valid_from is null or c.valid_from < ${ub.at}::timestamptz)`
          : tx`(c.valid_from is null or c.valid_from <= ${ub.at}::timestamptz)`,
      );
    }
    if (during?.from) parts.push(tx`(c.valid_to is null or c.valid_to >= ${during.from}::timestamptz)`);
    return parts.reduce((acc, part) => tx`${acc} and ${part}`);
  }
  return attrCondition(tx, cf.p, cf.f);
}

const likePattern = (text: string) => '%' + likeEscape(text) + '%';

/**
 * Text form of a scalar exactly as Postgres renders `value #>> '{}'` (= value_text) for values the
 * writer stored: strings verbatim, numbers and booleans as JSON.
 */
const scalarText = (v: string | number | boolean): string => (typeof v === 'string' ? v : JSON.stringify(v));

/**
 * Attribute comparisons, all through `c.value_text` (CONTRACTS §2a). eq / gte / lte compare
 * scalar values (texteq / text_ge / text_le are leakproof, so claim_value_text_idx serves them
 * under RLS); numeric bounds compare numerically. Array values are matched by `contains` only
 * (their value_text is JSON text), since element-wise matching cannot use an index under RLS.
 */
function attrCondition(tx: Tx, p: Predicate, a: AttrFilter) {
  const numeric = isNumeric(p);
  // Numeric text, or null for anything else, so a cast can never fail.
  const num = tx`(case when c.value_text ~ '^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$' then c.value_text::numeric end)`;
  const parts: Frag[] = [];
  if (a.eq !== undefined) parts.push(tx`c.value_text = ${scalarText(a.eq)}`);
  for (const [op, bound] of [['>=', a.gte], ['<=', a.lte]] as const) {
    if (bound === undefined) continue;
    const opSql = op === '>=' ? tx`>=` : tx`<=`;
    parts.push(
      numeric
        ? tx`${num} ${opSql} ${bound}::numeric`
        : tx`(left(c.value_text, 1) <> '[' and c.value_text ${opSql} ${String(bound)}::text)`,
    );
  }
  if (a.contains !== undefined) parts.push(tx`c.value_text ilike ${likePattern(a.contains)}`);
  return parts.reduce((acc, part) => tx`${acc} and ${part}`);
}

/**
 * Canonical ids of entities with a visible `eq` claim: an index lookup on claim_value_text_idx
 * that drives the query. The fold check in filterSource still decides whether it is effective.
 */
function attrEqCandidates(tx: Tx, p: Predicate, eq: string | number | boolean) {
  return tx`(
    select ${canonOf(tx, tx`c.subject`)} from claim c
    where c.predicate = ${p.id} and c.object_id is null and c.value_text = ${scalarText(eq)})`;
}

/**
 * Entities whose name (in any visible layer) contains `text`: public names from search_name
 * (trigram index, no RLS), private names from claim (RLS-filtered). Ids are canonical. This only
 * narrows candidates; the fold check in filterSource decides whether the *effective* name matches.
 */
function nameCandidates(tx: Tx, text: string) {
  const pattern = likePattern(text);
  return tx`(
    select ${canonOf(tx, tx`s.entity_id`)} from search_name s
    where s.name ilike ${pattern} and s.layer_id = any(rebox_visible())
    union
    select ${canonOf(tx, tx`c.subject`)} from claim c
    where c.layer_id = any(${privateVisibleLayers(tx)}) and c.predicate = 'name' and c.value_text ilike ${pattern})`;
}

/** Select-list expression returning the matched fact for reasons (or null). */
function reasonColumn(tx: Tx, cf: CompiledFilter) {
  return tx`(select jsonb_build_object(
      'value', c.value, 'other', case when c.other is null then null else ${canonOf(tx, tx`c.other`)} end,
      'from', c.valid_from, 'to', c.valid_to)
    ${filterSource(tx, cf, tx`e.id`)}
    order by c.valid_from desc nulls last, c.id desc limit 1)`;
}

interface Matched {
  value: unknown;
  other: string | null;
  from: string | null;
  to: string | null;
}

interface Row {
  id: string;
  kind: string;
  reasons: (Matched | null)[];
}

export async function runQuery(tx: Tx, plan: QueryPlan): Promise<QueryResult> {
  // Selective filters (name, attr eq) also contribute an index-driven candidate set. Each set is a
  // MATERIALIZED CTE so it is planned on its own (an index lookup on search_name_trgm_idx /
  // claim_value_text_idx) instead of being folded into the outer plan, where the planner may
  // prefer walking claim_layer_idx for the RLS condition.
  const candidateCtes: Frag[] = [];
  const conditions = plan.filters.map((cf, i) => {
    const check = tx`and exists (select 1 ${filterSource(tx, cf, tx`e.id`)})`;
    const cand =
      cf.type === 'name'
        ? nameCandidates(tx, cf.text)
        : cf.type === 'attr' && cf.f.eq !== undefined
          ? attrEqCandidates(tx, cf.p, cf.f.eq)
          : null;
    if (!cand) return check;
    const cte = tx(`cand_${i}`);
    candidateCtes.push(tx`, ${cte}(id) as materialized ${cand}`);
    return tx`and e.id in (select id from ${cte}) ${check}`;
  });
  const reasonCols = plan.filters.map((cf) => reasonColumn(tx, cf));
  const reasonsSql = reasonCols.length
    ? tx`jsonb_build_array(${reasonCols.reduce((acc, col) => tx`${acc}, ${col}`)})`
    : tx`'[]'::jsonb`;
  const after = plan.after ? tx`and e.id > ${plan.after}` : tx``;

  const rows = await tx<Row[]>`
    with recursive ${canonCte(tx)} ${candidateCtes.reduce((acc, cte) => tx`${acc} ${cte}`, tx``)}
    select e.id, e.kind, ${reasonsSql} as reasons
    from (
      select e.id, e.kind from entity e
      where e.kind = ${plan.find} and e.merged_into is null ${after} ${conditions.reduce((acc, c) => tx`${acc} ${c}`, tx``)}
      order by e.id
      limit ${plan.limit + 1}
    ) e
    order by e.id`;

  const page = rows.slice(0, plan.limit);
  const refIds = page.flatMap((r) => [r.id, ...r.reasons.flatMap((m) => (m?.other ? [m.other] : []))]);
  const refs = await entityRefs(tx, refIds);
  return {
    results: page.map((r) => ({
      id: r.id,
      kind: r.kind,
      name: refs.get(r.id)?.name ?? null,
      reasons: plan.filters.map((cf, i) => formatReason(cf, r.reasons[i] ?? null, (id) => refs.get(id)?.name ?? id)),
    })),
    next: rows.length > plan.limit ? encodeCursor({ id: page[page.length - 1]!.id }) : null,
  };
}

// ── Reasons ─────────────────────────────────────────────────────────────────────────────────

const year = (ts: string | null) => (ts ? ts.slice(0, 4) : null);

export function formatSpan(from: string | null, to: string | null): string {
  const a = year(from);
  const b = year(to);
  if (!a && !b) return '';
  if (a && b) return a === b ? ` ${a}` : ` ${a}–${b}`;
  return a ? ` ${a}–` : ` –${b}`;
}

function valueText(v: unknown): string {
  if (Array.isArray(v)) return v.map(valueText).join(', ');
  return typeof v === 'string' ? v : JSON.stringify(v);
}

export function formatReason(cf: CompiledFilter, m: Matched | null, nameOf: (id: string) => string): string {
  if (cf.type === 'name') return m ? `name ${valueText(m.value)}` : `name ~ ${cf.text}`;
  if (!m) return cf.p.id;
  if (cf.type === 'attr') return `${cf.p.id} ${valueText(m.value)}`;
  const other = m.other ? nameOf(m.other) : '?';
  const span = formatSpan(m.from, m.to);
  return cf.f.dir === 'out' ? `${cf.p.id} ${other}${span}` : `${other} ${cf.p.id}${span}`;
}
