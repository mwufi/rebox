/** Timeline display helpers (pure; tested in test/timeline.test.tsx). */
import type { TimelineEvent } from '../api';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** How exact an event's date is, from the API's `precision` or inferred from the timestamp. */
export function precisionOf(e: Pick<TimelineEvent, 'at' | 'precision' | 'predicate'>): 'year' | 'day' | 'time' {
  if (e.precision) return e.precision;
  if (e.predicate === 'founded_year') return 'year';
  if (e.at.slice(11, 19) !== '00:00:00') return 'time';
  return e.at.slice(5, 10) === '01-01' ? 'year' : 'day';
}

/** "2017", "Jun 28", or "Jun 28" (+ time in the title attribute): the year is the group label. */
export function eventDate(e: Pick<TimelineEvent, 'at' | 'precision' | 'predicate'>): { short: string; full: string } {
  const y = e.at.slice(0, 4);
  const p = precisionOf(e);
  if (p === 'year') return { short: '', full: y };
  const m = MONTHS[Number(e.at.slice(5, 7)) - 1] ?? '';
  const d = String(Number(e.at.slice(8, 10)));
  const full = `${m} ${d}, ${y}`;
  return { short: `${m} ${d}`, full: p === 'time' ? `${full} · ${e.at.slice(11, 16)} UTC` : full };
}

export interface YearGroup {
  year: string;
  events: TimelineEvent[];
}

/** Consecutive events grouped by year (input is newest first; order is kept). */
export function groupByYear(events: TimelineEvent[]): YearGroup[] {
  const out: YearGroup[] = [];
  for (const e of events) {
    const y = e.at.slice(0, 4);
    const last = out[out.length - 1];
    if (last && last.year === y) last.events.push(e);
    else out.push({ year: y, events: [e] });
  }
  return out;
}

/** Launches, funding, foundings and acquisitions read as headline events. */
export function isHeadline(e: Pick<TimelineEvent, 'kind' | 'headline'>): boolean {
  if (typeof e.headline === 'boolean') return e.headline;
  return e.kind === 'launched' || e.kind === 'founded' || e.kind === 'acquired';
}

export const eventKey = (e: TimelineEvent, i: number) => e.key ?? `${e.at}|${e.kind}|${e.title}|${i}`;

/** One report behind a milestone event (S18; `sources` on milestone events). */
export interface EventSource {
  url: string;
  title: string;
  layer: string;
}

/** A timeline event as the API sends it, with the S18 milestone fields. */
export type SourcedEvent = TimelineEvent & { sources?: EventSource[]; support?: number };

const isHttp = (u: unknown): u is string => typeof u === 'string' && /^https?:\/\//i.test(u);

/** The milestone's sources (http(s) urls only), oldest first; a pre-S18 milestone's own url. */
export function eventSources(e: SourcedEvent): EventSource[] {
  if (Array.isArray(e.sources)) return e.sources.filter((s) => isHttp(s?.url));
  const v = e.fact?.value;
  const url = v && typeof v === 'object' ? (v as { url?: unknown }).url : null;
  return e.kind === 'milestone' && isHttp(url) ? [{ url, title: e.title, layer: e.fact?.layer ?? '' }] : [];
}

/** How many distinct sources report the event (1 unless it is a cluster of reports). */
export function sourceCount(e: SourcedEvent): number {
  return typeof e.support === 'number' && e.support > 0 ? e.support : Math.max(1, eventSources(e).length);
}

/** A milestone several sources reported: shown once, with "N sources". */
export const isCluster = (e: SourcedEvent): boolean => e.kind === 'milestone' && sourceCount(e) > 1;

/** "techcrunch.com" for a source link label. */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}
