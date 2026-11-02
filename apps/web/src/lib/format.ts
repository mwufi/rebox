import type { Fact } from '../api';

/** Year (or year-month when it is not January 1st) of an ISO timestamp. */
export function shortDate(iso: string | null): string | null {
  if (!iso) return null;
  const y = iso.slice(0, 4);
  const md = iso.slice(5, 10);
  if (!md || md === '01-01') return y;
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const m = months[Number(iso.slice(5, 7)) - 1];
  return m ? `${m} ${y}` : y;
}

/**
 * Predicates that happen at one moment (a launch, a post, an acquisition, a milestone): they show
 * one date, never a range running on to "now".
 */
export const POINT_IN_TIME: ReadonlySet<string> = new Set(['launched', 'posted', 'attended', 'milestone', 'acquired', 'invested_in']);

/** The date text for a fact row, and whether it is an open (current) range. */
export function factWhen(f: { predicate: string; valid_from: string | null; valid_to: string | null }): { text: string; current: boolean } {
  if (POINT_IN_TIME.has(f.predicate)) return { text: shortDate(f.valid_from ?? f.valid_to) ?? '', current: false };
  return { text: dateRange(f.valid_from, f.valid_to), current: Boolean(f.valid_from && !f.valid_to) };
}

/** "2019 – 2021", "2019 –", "– 2021", or "" when the fact has no validity. */
export function dateRange(from: string | null, to: string | null): string {
  const a = shortDate(from);
  const b = shortDate(to);
  if (!a && !b) return '';
  if (a && b) return a === b ? a : `${a} – ${b}`;
  return a ? `${a} –` : `– ${b}`;
}

export function valueText(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (Array.isArray(v)) return v.map(valueText).join(', ');
  if (typeof v === 'string') return v;
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('en-US', { useGrouping: v >= 10000 }) : String(v);
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  return JSON.stringify(v);
}

/** Edge props ({"title": "CTO"}) as short text. */
export function edgeProps(v: unknown): string {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return '';
  return Object.entries(v as Record<string, unknown>)
    .filter(([, x]) => x !== null && x !== undefined && x !== '')
    .map(([k, x]) => (k === 'title' || k === 'role' ? valueText(x) : `${k} ${valueText(x)}`))
    .join(' · ');
}

const SOURCE_CODES: Record<string, string> = {
  wikidata: 'WD',
  hn: 'HN',
  github: 'GH',
  x: 'X',
  manual: 'MN',
  yc: 'YC',
  producthunt: 'PH',
  crunchbase: 'CB',
  linkedin: 'LI',
  gmail: 'GM',
  crm: 'CRM',
};

/** Short code for a source, readable at a glance in a badge. */
export function sourceCode(source: string): string {
  return SOURCE_CODES[source] ?? source.replace(/[^a-z0-9]/gi, '').slice(0, 3).toUpperCase();
}

/** Stable hue per source for the badge pip (known sources get fixed hues). */
export function sourceHue(source: string): number {
  const fixed: Record<string, number> = { wikidata: 205, hn: 24, github: 265, x: 0, manual: 45, yc: 18 };
  if (source in fixed) return fixed[source]!;
  let h = 0;
  for (const ch of source) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

export const scopeOf = (layer: string) => layer.split('/')[0] ?? 'public';

export function provenance(f: Pick<Fact, 'layer' | 'source_ref' | 'updated_at'>): string {
  return `${f.layer}\n${f.source_ref}\nupdated ${f.updated_at.slice(0, 10)}`;
}

export function isUrlLike(s: string): boolean {
  return /^https?:\/\//i.test(s);
}

export function hrefFor(predicate: string, value: unknown): string | null {
  if (typeof value !== 'string') return null;
  if (isUrlLike(value)) return value;
  switch (predicate) {
    case 'domain':
      return `https://${value}`;
    case 'github':
      return `https://github.com/${value}`;
    case 'x_handle':
      return `https://x.com/${value}`;
    case 'hn_user':
      return `https://news.ycombinator.com/user?id=${encodeURIComponent(value)}`;
    case 'hn_story':
      return /^\d+$/.test(value) ? `https://news.ycombinator.com/item?id=${value}` : null;
    case 'wikidata':
      return /^Q\d+$/.test(value) ? `https://www.wikidata.org/wiki/${value}` : null;
    default:
      return null;
  }
}

export function relativeTime(iso: string, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

export const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;

export const KINDS = ['person', 'company', 'launch', 'event', 'place'] as const;

export const kindLabel = (k: string, n = 2) =>
  n === 1 ? k : ({ person: 'people', company: 'companies', launch: 'launches' } as Record<string, string>)[k] ?? `${k}s`;

/** Fallback when an entity has no name. */
export const displayName = (ref: { id: string; name: string | null }) => ref.name ?? ref.id.split(':').slice(2).join(':');

export type HandleKind = 'hn' | 'x';

/**
 * Whether an entity's only name is a handle (its name equals its HN user or X handle). Uses the
 * API's `handle` hint on refs, or the view's own identity facts.
 */
export function handleOf(e: {
  name: string | null;
  handle?: HandleKind | null;
  facts?: { predicate: string; value: unknown }[];
}): HandleKind | null {
  if (e.handle === 'hn' || e.handle === 'x') return e.handle;
  if (!e.name || !e.facts) return null;
  const n = e.name.replace(/^@/, '').toLowerCase();
  const has = (p: string) => e.facts!.some((f) => f.predicate === p && typeof f.value === 'string' && f.value.toLowerCase() === n);
  if (has('hn_user')) return 'hn';
  if (has('x_handle')) return 'x';
  return null;
}

export const HANDLE_LABEL: Record<HandleKind, string> = { hn: 'HN handle', x: 'X handle' };

/** A safe http(s) avatar URL from a ref (`avatar`) or a view's `avatar` fact; else null. */
export function avatarOf(e: { avatar?: string | null; facts?: { predicate: string; value: unknown }[] }): string | null {
  const v = e.avatar ?? e.facts?.find((f) => f.predicate === 'avatar')?.value;
  return typeof v === 'string' && /^https?:\/\/\S+$/i.test(v) ? v : null;
}

/** One or two initials for an avatar placeholder. */
export function initials(name: string): string {
  const words = name.replace(/^@/, '').split(/[\s._-]+/).filter(Boolean);
  const a = words[0]?.[0] ?? '?';
  const b = words.length > 1 ? words[words.length - 1]![0] : '';
  return (a + (b ?? '')).toUpperCase();
}
