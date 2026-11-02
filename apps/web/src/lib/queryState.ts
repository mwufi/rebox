/**
 * Query builder state <-> URL <-> CONTRACTS §6 query body. Pure; tested in test/queryState.test.ts.
 *
 * URL form (shareable, readable):  /query?find=person&w=name~chen&w=edge~worked_at~out~rbx:company:x~2018-01-01~2021-12-31
 *   name~<text>
 *   edge~<predicate>~<out|in>~<to id?>~<from date?>~<to date?>
 *   attr~<predicate>~<eq|gte|lte|contains>~<value>
 * Components are percent-encoded, with `~` itself escaped, so any text round-trips.
 */
import type { Filter, Predicate, Query } from '../api';

export type AttrOp = 'eq' | 'gte' | 'lte' | 'contains';
export const ATTR_OPS: AttrOp[] = ['eq', 'contains', 'gte', 'lte'];

export type Row =
  | { type: 'name'; text: string }
  | { type: 'edge'; predicate: string; dir: 'out' | 'in'; to: string; from: string; until: string }
  | { type: 'attr'; predicate: string; op: AttrOp; value: string };

export interface BuilderState {
  find: string;
  rows: Row[];
}

const SEP = '~';
const encodePart = (s: string) => encodeURIComponent(s).replace(/~/g, '%7E');
const decodePart = (s: string) => {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
};

export function encodeRow(r: Row): string {
  const parts =
    r.type === 'name'
      ? ['name', r.text]
      : r.type === 'edge'
        ? ['edge', r.predicate, r.dir, r.to, r.from, r.until]
        : ['attr', r.predicate, r.op, r.value];
  // Drop trailing empty components so URLs stay short.
  while (parts.length > 2 && parts[parts.length - 1] === '') parts.pop();
  return parts.map(encodePart).join(SEP);
}

export function decodeRow(s: string): Row | null {
  const [type, ...rest] = s.split(SEP).map(decodePart);
  const at = (i: number) => rest[i] ?? '';
  if (type === 'name') return { type, text: at(0) };
  if (type === 'edge') {
    const dir = at(1) === 'in' ? 'in' : 'out';
    return { type, predicate: at(0), dir, to: at(2), from: at(3), until: at(4) };
  }
  if (type === 'attr') {
    const op = (ATTR_OPS as string[]).includes(at(1)) ? (at(1) as AttrOp) : 'eq';
    return { type, predicate: at(0), op, value: at(2) };
  }
  return null;
}

export const DEFAULT_STATE: BuilderState = { find: 'person', rows: [] };

export function stateToParams(s: BuilderState): URLSearchParams {
  const p = new URLSearchParams();
  p.set('find', s.find);
  for (const r of s.rows) p.append('w', encodeRow(r));
  return p;
}

export function paramsToState(p: URLSearchParams): BuilderState {
  const find = p.get('find') || DEFAULT_STATE.find;
  const rows = p.getAll('w').flatMap((w) => {
    const r = decodeRow(w);
    return r ? [r] : [];
  });
  return { find, rows };
}

const isNumericPred = (p: Predicate | undefined) => p?.meta.value_type === 'int' || p?.meta.value_type === 'number';

/** Whether a row has enough input to become a filter. */
export function rowComplete(r: Row): boolean {
  if (r.type === 'name') return r.text.trim() !== '';
  if (r.type === 'edge') return r.predicate !== '';
  return r.predicate !== '' && r.value.trim() !== '';
}

/** The §6 query body. Incomplete rows are skipped; numeric attributes get numeric bounds. */
export function stateToQuery(s: BuilderState, preds: ReadonlyMap<string, Predicate>, limit = 50): Query {
  const where: Filter[] = s.rows.filter(rowComplete).map((r): Filter => {
    if (r.type === 'name') return { name: r.text.trim() };
    if (r.type === 'edge') {
      const edge: Extract<Filter, { edge: unknown }>['edge'] = { predicate: r.predicate, dir: r.dir };
      if (r.to) edge.to = r.to;
      if (r.from || r.until) edge.during = { ...(r.from ? { from: r.from } : {}), ...(r.until ? { to: r.until } : {}) };
      return { edge };
    }
    const raw = r.value.trim();
    const numeric = isNumericPred(preds.get(r.predicate)) && r.op !== 'contains' && /^-?\d+(\.\d+)?$/.test(raw);
    return { attr: { predicate: r.predicate, [r.op]: numeric ? Number(raw) : raw } };
  });
  return { find: s.find, where, limit };
}

/** Human summary of a row, used for the page title and result header. */
export function describeRow(r: Row, nameOf: (id: string) => string = (id) => id): string {
  if (r.type === 'name') return `name contains “${r.text}”`;
  if (r.type === 'edge') {
    const target = r.to ? nameOf(r.to) : 'anything';
    const span = r.from || r.until ? ` during ${r.from || '…'} – ${r.until || '…'}` : '';
    return r.dir === 'out' ? `${r.predicate} → ${target}${span}` : `${target} → ${r.predicate} → it${span}`;
  }
  const sym = { eq: '=', gte: '≥', lte: '≤', contains: '∋' }[r.op];
  return `${r.predicate} ${sym} ${r.value}`;
}

export function newRow(type: Row['type'], preds: Predicate[], find: string): Row {
  if (type === 'name') return { type, text: '' };
  if (type === 'edge') {
    const p = edgePredicatesFor(preds, find)[0];
    return { type, predicate: p?.p.id ?? '', dir: p?.dir ?? 'out', to: '', from: '', until: '' };
  }
  const p = attrPredicatesFor(preds, find)[0];
  return { type, predicate: p?.id ?? '', op: 'eq', value: '' };
}

/** Edge predicates usable from `find`: outgoing when find is a subject kind, incoming when it is the object kind. */
export function edgePredicatesFor(preds: Predicate[], find: string): { p: Predicate; dir: 'out' | 'in' }[] {
  const out: { p: Predicate; dir: 'out' | 'in' }[] = [];
  for (const p of preds) {
    if (!p.object_kind || p.meta.private || !p.meta.filterable) continue;
    if (p.subject_kinds.includes(find)) out.push({ p, dir: 'out' });
    if (p.object_kind === find) out.push({ p, dir: 'in' });
  }
  return out;
}

export function attrPredicatesFor(preds: Predicate[], find: string): Predicate[] {
  return preds.filter((p) => !p.object_kind && !p.meta.private && p.id !== 'name' && p.subject_kinds.includes(find) && p.meta.filterable);
}
