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
import { entityRefs } from './entities.js';
import { validation, type ErrorDetail } from './errors.js';
import { canonCte, likeEscape } from './sqlfrag.js';
import { isEdge, type Predicate, type Vocab } from './vocab.js';

export const MAX_FILTERS = 10;
export const MAX_LIMIT = 200;

const ISO_DAY = /^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+Z-]+)?$/;
const scalar = z.union([z.string(), z.number(), z.boolean()]);

const nameFilter = z.object({ name: z.string().trim().min(1).max(200) }).strict();
const edgeFilter = z
  .object({
    edge: z
      .object({
        predicate: z.string().min(1),
        dir: z.enum(['out', 'in']).default('out'),
        to: z.string().min(1).nullish(),
        during: z
          .object({ from: z.string().regex(ISO_DAY).nullish(), to: z.string().regex(ISO_DAY).nullish() })
          .strict()
          .nullish(),
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
      } else {
        for (const op of ['gte', 'lte'] as const) {
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

/**
 * `from claim c join layer l ... where <cond>` for one filter, anchored on outer `e.id`.
 * The anti-join on c2 is the fold expressed per row: c must be the winner for its key.
 */
function filterSource(tx: Tx, cf: CompiledFilter) {
  const dir = cf.type === 'edge' ? cf.f.dir : 'out';
  const anchor = dir === 'out' ? tx`c.subject` : tx`c.object_id`;
  const anchor2 = dir === 'out' ? tx`c2.subject` : tx`c2.object_id`;
  const other = dir === 'out' ? tx`c.object_id` : tx`c.subject`;
  const other2 = dir === 'out' ? tx`c2.object_id` : tx`c2.subject`;
  const predicate = cf.type === 'name' ? 'name' : cf.p.id;
  const inGroup = (col: Frag) => tx`(${col} = e.id or ${col} in (select cn.member from canon cn where cn.root = e.id))`;

  let sameKey = tx``;
  if (cf.type === 'edge') sameKey = tx`and ${canonOf(tx, other2)} = ${canonOf(tx, other)}`;
  else if (cf.type === 'attr' && cf.p.cardinality === 'many') sameKey = tx`and c2.value = c.value`;

  return tx`
    from claim c join layer l on l.id = c.layer_id
    where c.predicate = ${predicate} and ${inGroup(anchor)} and not c.retract
      and ${matchCondition(tx, cf, other)}
      and not exists (
        select 1 from claim c2 join layer l2 on l2.id = c2.layer_id
        where c2.predicate = c.predicate and ${inGroup(anchor2)} ${sameKey}
          and (l2.scope, c2.updated_at, c2.id) > (l.scope, c.updated_at, c.id)
      )`;
}

/** The filter's own predicate on the claim `c` (value / object / validity). */
function matchCondition(tx: Tx, cf: CompiledFilter, other: Frag) {
  if (cf.type === 'name') {
    return tx`(c.value #>> '{}') ilike ${'%' + likeEscape(cf.text) + '%'}`;
  }
  if (cf.type === 'edge') {
    const { to, during } = cf.f;
    // The other side must be visible (RLS on entity), as in the fold.
    const parts = [tx`exists (select 1 from entity oe where oe.id = ${canonOf(tx, other)})`];
    if (to) parts.push(tx`${canonOf(tx, other)} = ${canonOf(tx, tx`${to}::text`)}`);
    if (during?.to) parts.push(tx`(c.valid_from is null or c.valid_from <= ${during.to}::timestamptz)`);
    if (during?.from) parts.push(tx`(c.valid_to is null or c.valid_to >= ${during.from}::timestamptz)`);
    return parts.reduce((acc, part) => tx`${acc} and ${part}`);
  }
  // attr: compare each scalar element (attribute values may be arrays).
  const a = cf.f;
  const numeric = isNumeric(cf.p);
  const parts = [];
  if (a.eq !== undefined) parts.push(tx`c.value @> ${tx.json(a.eq)}::jsonb`);
  const el = (cmp: Frag) => tx`exists (
      select 1 from jsonb_array_elements(case when jsonb_typeof(c.value) = 'array' then c.value
                                              else jsonb_build_array(c.value) end) el(v)
      where ${cmp})`;
  for (const [op, bound] of [['>=', a.gte], ['<=', a.lte]] as const) {
    if (bound === undefined) continue;
    const opSql = op === '>=' ? tx`>=` : tx`<=`;
    parts.push(
      numeric
        ? el(tx`jsonb_typeof(el.v) = 'number' and (el.v #>> '{}')::numeric ${opSql} ${bound}::numeric`)
        : el(tx`jsonb_typeof(el.v) = 'string' and (el.v #>> '{}') collate "C" ${opSql} ${String(bound)}::text collate "C"`),
    );
  }
  if (a.contains !== undefined) {
    parts.push(el(tx`jsonb_typeof(el.v) = 'string' and (el.v #>> '{}') ilike ${'%' + likeEscape(a.contains) + '%'}`));
  }
  return parts.reduce((acc, part) => tx`${acc} and ${part}`);
}

/** Select-list expression returning the matched fact for reasons (or null). */
function reasonColumn(tx: Tx, cf: CompiledFilter) {
  const other = cf.type === 'edge' && cf.f.dir === 'in' ? tx`c.subject` : tx`c.object_id`;
  return tx`(select jsonb_build_object(
      'value', c.value, 'other', case when c.object_id is null then null else ${canonOf(tx, other)} end,
      'from', c.valid_from, 'to', c.valid_to)
    ${filterSource(tx, cf)}
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
  const exists = plan.filters.map((cf) => tx`and exists (select 1 ${filterSource(tx, cf)})`);
  const reasonCols = plan.filters.map((cf) => reasonColumn(tx, cf));
  const reasonsSql = reasonCols.length
    ? tx`jsonb_build_array(${reasonCols.reduce((acc, col) => tx`${acc}, ${col}`)})`
    : tx`'[]'::jsonb`;
  const after = plan.after ? tx`and e.id > ${plan.after}` : tx``;

  const rows = await tx<Row[]>`
    with recursive ${canonCte(tx)}
    select e.id, e.kind, ${reasonsSql} as reasons
    from (
      select e.id, e.kind from entity e
      where e.kind = ${plan.find} and e.merged_into is null ${after} ${exists}
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
