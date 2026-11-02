/**
 * Entity timelines (CONTRACTS §14): the dated history of an entity, newest first.
 *
 * Built from the fold (so RLS, merges and precedence apply exactly as on the entity view):
 * - outgoing edges with valid_from / valid_to: "joined" / "left" (or one event for point-in-time
 *   predicates such as `launched`, `posted`, `attended`, `acquired`);
 * - `milestone` attributes (dated by valid_from), `founded_year`, and any attribute predicate
 *   marked `meta.timeline` that carries a date;
 * - dated incoming edges (people joining a company, "launched by", "acquired by");
 * - for companies: dated launches posted by its founders that the company itself has no
 *   `launched` edge for;
 * - "first seen in Rebox": the first visible insert in the change feed.
 *
 * Milestones are clustered (S18, `clusterMilestones`): the same "$9.7M raise" reported by ten
 * outlets over three weeks is one event with ten `sources`, not ten events.
 */
import type { Tx } from './db.js';
import type { EntityRef } from './entities.js';
import { foldFacts, type FoldedRow } from './fold.js';
import { toFacts, type Fact } from './views.js';
import type { Vocab } from './vocab.js';

export type EventKind =
  | 'founded'
  | 'joined'
  | 'left'
  | 'started'
  | 'ended'
  | 'launched'
  | 'posted'
  | 'attended'
  | 'acquired'
  | 'invested'
  | 'milestone'
  | 'first_seen';

export type Precision = 'year' | 'day' | 'time';

export interface TimelineEvent {
  /** Stable key, unique within the timeline (paging cursor, UI list key). */
  key: string;
  at: string;
  kind: EventKind;
  title: string;
  predicate: string | null;
  other: EntityRef | null;
  /** The folded fact the event comes from (null for first_seen). */
  fact: Fact | null;
  /** Which side of the fact this entity is on. */
  dir: 'out' | 'in' | null;
  /** How exact `at` is: a year (Jan 1st / founded_year), a calendar day, or a timestamp. */
  precision: Precision;
  /** Launches, funding, acquisitions and foundings: shown as headline events. */
  headline: boolean;
  /** Further entities on the same event (e.g. every founder on "Founded by A and B"). */
  others?: EntityRef[];
  /** first_seen only: the layer of the first claim. */
  layer?: string;
  /**
   * `milestone` events only: every article/post behind the event, oldest first, one per url
   * (a cluster of reports of the same milestone, or the one source of a single milestone).
   */
  sources?: EventSource[];
  /** `milestone` events only: number of distinct sources (urls) reporting it; 1 for a single one. */
  support?: number;
}

export interface EventSource {
  url: string;
  title: string;
  layer: string;
}

/** Edge predicates that happen at one moment: one event, never a range. */
export const POINT_IN_TIME_EDGES: ReadonlySet<string> = new Set(['launched', 'posted', 'attended', 'acquired', 'invested_in']);

/** Never on timelines (social graph noise, not history). */
const EXCLUDED: ReadonlySet<string> = new Set(['follows', 'name']);

/** Caps that keep hub entities cheap. */
const MAX_FOUNDERS = 50;

const iso = (d: Date) => d.toISOString();

export function precisionOf(at: string, predicate: string | null): Precision {
  if (predicate === 'founded_year') return 'year';
  if (at.slice(11, 23) !== '00:00:00.000') return 'time';
  return at.slice(5, 10) === '01-01' ? 'year' : 'day';
}

const MILESTONE_HEADLINE = /fund|rais|round|seed|series|launch|acqui|ipo|exit/i;

function milestoneValue(v: unknown): { title: string | null; kind: string | null; url: string | null } {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return { title: typeof v === 'string' ? v : null, kind: null, url: null };
  const o = v as Record<string, unknown>;
  const s = (x: unknown) => (typeof x === 'string' && x.trim() ? x.trim() : null);
  return { title: s(o.title), kind: s(o.kind), url: s(o.url) };
}

const edgeTitle = (v: unknown): string | null => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const t = (v as Record<string, unknown>).title ?? (v as Record<string, unknown>).role;
  return typeof t === 'string' && t.trim() ? t.trim() : null;
};

type Phase = 'start' | 'end' | 'point';

/** Event kind + English title for one side of a dated edge. `other` is the other entity's name. */
export function describeEdge(
  predicate: string,
  dir: 'out' | 'in',
  phase: Phase,
  other: string,
  value: unknown,
  label: (p: string) => string,
): { kind: EventKind; title: string } {
  const role = edgeTitle(value);
  const as = role ? ` as ${role}` : '';
  const out = dir === 'out';
  switch (predicate) {
    case 'worked_at':
      if (phase === 'end') return { kind: 'left', title: out ? `Left ${other}` : `${other} left` };
      return { kind: 'joined', title: out ? `Joined ${other}${as}` : `${other} joined${as}` };
    case 'ceo_of':
      if (phase === 'end') return { kind: 'left', title: out ? `Stepped down as CEO of ${other}` : `${other} stepped down as CEO` };
      return { kind: 'joined', title: out ? `Became CEO of ${other}` : `${other} became CEO` };
    case 'founded':
      if (phase === 'end') return { kind: 'ended', title: out ? `Left ${other} (co-founder)` : `Co-founder ${other} left` };
      return { kind: 'founded', title: out ? `Founded ${other}` : `Founded by ${other}` };
    case 'launched':
      return { kind: 'launched', title: out ? `Launched ${other}` : `Launched by ${other}` };
    case 'posted':
      return { kind: 'posted', title: out ? `Posted ${other}` : `Posted by ${other}` };
    case 'attended':
      return { kind: 'attended', title: out ? `Attended ${other}${as}` : `${other} attended${as}` };
    case 'acquired':
      return { kind: 'acquired', title: out ? `Acquired ${other}` : `Acquired by ${other}` };
    case 'invested_in':
      return { kind: 'invested', title: out ? `Invested in ${other}` : `${other} invested` };
    case 'based_in':
      if (phase === 'end') return { kind: 'ended', title: out ? `Left ${other}` : `${other} moved away` };
      return { kind: 'started', title: out ? `Based in ${other}` : `${other} moved here` };
    default: {
      const l = label(predicate);
      if (phase === 'end') return { kind: 'ended', title: out ? `${l} ${other} ended` : `${other}: ${l.toLowerCase()} ended` };
      return { kind: 'started', title: out ? `${l} ${other}` : `${other}: ${l.toLowerCase()}` };
    }
  }
}

const HEADLINE_KINDS: ReadonlySet<EventKind> = new Set(['founded', 'launched', 'acquired']);

/** Edge rows (folded) that carry at least one date and belong on a timeline. */
const datedEdges = (rows: FoldedRow[]) =>
  rows.filter((r) => r.other !== null && !EXCLUDED.has(r.predicate) && (r.valid_from !== null || r.valid_to !== null));

function edgeEvents(facts: Fact[], rows: FoldedRow[], dir: 'out' | 'in', label: (p: string) => string, keyPrefix = dir): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  rows.forEach((r, i) => {
    const fact = facts[i]!;
    const other = dir === 'out' ? fact.object : fact.subject;
    const name = other?.name ?? other?.id ?? '?';
    const base = `${keyPrefix}|${r.predicate}|${r.other}|${r.layer_id}`;
    const push = (phase: Phase, at: Date) => {
      const d = describeEdge(r.predicate, dir, phase, name, r.value, label);
      const atIso = iso(at);
      events.push({
        key: `${base}|${phase}`,
        at: atIso,
        kind: d.kind,
        title: d.title,
        predicate: r.predicate,
        other,
        fact,
        dir,
        precision: precisionOf(atIso, r.predicate),
        headline: HEADLINE_KINDS.has(d.kind) || (d.kind === 'posted' && dir === 'in'),
      });
    };
    if (POINT_IN_TIME_EDGES.has(r.predicate)) {
      const at = r.valid_from ?? r.valid_to;
      if (at) push('point', at);
      return;
    }
    if (r.valid_from) push('start', r.valid_from);
    if (r.valid_to) push('end', r.valid_to);
  });
  return events;
}

/** `founded_year` → ISO Jan 1st of that year, or null. */
export function yearAt(v: unknown): string | null {
  const y = typeof v === 'number' ? v : typeof v === 'string' && /^\d{4}$/.test(v) ? Number(v) : NaN;
  if (!Number.isInteger(y) || y < 1000 || y > 9999) return null;
  return `${String(y)}-01-01T00:00:00.000Z`;
}

/** "A", "A and B", "A, B and C", "A, B and 3 others". */
export function joinNames(names: string[], max = 3): string {
  if (names.length <= 1) return names[0] ?? '';
  if (names.length > max) return `${names.slice(0, max - 1).join(', ')} and ${names.length - (max - 1)} others`;
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

function attributeEvents(facts: Fact[], rows: FoldedRow[], vocab: Vocab, founders: EntityRef[] = []): TimelineEvent[] {
  const events: TimelineEvent[] = [];
  rows.forEach((r, i) => {
    const fact = facts[i]!;
    if (r.predicate === 'founded_year') {
      const at = yearAt(r.value);
      if (!at) return;
      const names = founders.map((f) => f.name ?? f.id);
      events.push({
        key: `attr|founded_year|${r.layer_id}`,
        at,
        kind: 'founded',
        title: names.length ? `Founded by ${joinNames(names)}` : 'Founded',
        predicate: r.predicate,
        other: founders[0] ?? null,
        ...(founders.length > 1 ? { others: founders } : {}),
        fact,
        dir: 'out',
        precision: 'year',
        headline: true,
      });
      return;
    }
    const at = r.valid_from ?? r.valid_to;
    if (!at) return;
    if (r.predicate === 'milestone') {
      const m = milestoneValue(r.value);
      const atIso = iso(at);
      events.push({
        key: `attr|milestone|${r.layer_id}|${JSON.stringify(r.value)}`,
        at: atIso,
        kind: 'milestone',
        title: m.title ?? 'Milestone',
        predicate: r.predicate,
        other: null,
        fact,
        dir: 'out',
        precision: precisionOf(atIso, r.predicate),
        headline: m.kind !== null && MILESTONE_HEADLINE.test(m.kind),
      });
      return;
    }
    const p = vocab.predicates.get(r.predicate);
    if (!(p?.meta as { timeline?: boolean } | undefined)?.timeline) return;
    const atIso = iso(at);
    events.push({
      key: `attr|${r.predicate}|${r.layer_id}|${JSON.stringify(r.value)}`,
      at: atIso,
      kind: 'milestone',
      title: `${p?.meta.label ?? r.predicate}: ${typeof r.value === 'string' ? r.value : JSON.stringify(r.value)}`,
      predicate: r.predicate,
      other: null,
      fact,
      dir: 'out',
      precision: precisionOf(atIso, r.predicate),
      headline: false,
    });
  });
  return events;
}

/** Newest first; ties by key so paging is stable. */
export function compareEvents(a: Pick<TimelineEvent, 'at' | 'key'>, b: Pick<TimelineEvent, 'at' | 'key'>): number {
  if (a.at !== b.at) return a.at < b.at ? 1 : -1;
  return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
}

/**
 * Every event for an un-merged, visible entity `id` under the transaction's view, sorted newest
 * first. Callers resolve merges (canonicalIds) first.
 */
export async function buildTimeline(tx: Tx, id: string, kind: string, vocab: Vocab): Promise<TimelineEvent[]> {
  const label = (p: string) => vocab.predicates.get(p)?.meta.label ?? p.replace(/_/g, ' ');
  // One round trip: the merge group, which incoming predicates carry dates at all (a cheap
  // probe on claim_object_idx, so a hub with thousands of undated incoming edges never folds
  // them), and the first visible insert in the change feed.
  const [probe] = await tx<{ dated_in: string[]; first: { at: string; layer_id: string; predicate: string | null } | null }[]>`
    with recursive grp(id) as (
      select ${id}::text
      union
      select e.id from entity e join grp g on e.merged_into = g.id
    )
    select
      array(select distinct c.predicate from claim c
            where c.object_id in (select id from grp) and (c.valid_from is not null or c.valid_to is not null)) as dated_in,
      (select jsonb_build_object('at', f.at, 'layer_id', f.layer_id, 'predicate', f.predicate)
       from grp g cross join lateral (
         -- per member: a range scan on change_subject_seq_idx (0007), stopping at the first hit
         select ch.seq, ch.at, ch.layer_id, ch.predicate from change ch
         where ch.subject = g.id and ch.op = 'insert' and ch.layer_id = any(rebox_visible())
         order by ch.seq limit 1) f
       order by f.seq limit 1) as first`;
  const inPreds = (probe?.dated_in ?? []).filter((p) => !EXCLUDED.has(p));
  if (kind === 'company' && !inPreds.includes('founded')) inPreds.push('founded');
  const [out, incAll] = await Promise.all([
    foldFacts(tx, { roots: [id], dir: 'out' }),
    inPreds.length ? foldFacts(tx, { roots: [id], dir: 'in', predicates: inPreds }) : Promise.resolve([] as FoldedRow[]),
  ]);
  const founderRows = kind === 'company' ? incAll.filter((r) => r.predicate === 'founded') : [];
  const inc = incAll.filter((r) => r.predicate !== 'founded' || r.valid_from || r.valid_to);
  const first = probe?.first ? [{ ...probe.first, at: new Date(probe.first.at) }] : [];

  // Launches posted by the company's founders that the company has no `launched` edge for.
  const launchedHere = new Set(out.filter((r) => r.predicate === 'launched').map((r) => r.other));
  const founders = [...new Set(founderRows.map((r) => r.other!))].slice(0, MAX_FOUNDERS);
  // Undated `founded` edges take the company's founding year ("founded with founding year").
  const undatedFounded = out.filter((r) => r.predicate === 'founded' && r.other && !r.valid_from && !r.valid_to);
  const companies = undatedFounded.map((r) => r.other!);
  const extra =
    founders.length || companies.length
      ? await foldFacts(tx, { roots: [...founders, ...companies], dir: 'out', predicates: ['posted', 'founded_year'] })
      : [];
  const founderSet = new Set(founders);
  const posted = extra.filter(
    (r) => r.predicate === 'posted' && founderSet.has(r.root) && (r.valid_from || r.valid_to) && !launchedHere.has(r.other),
  );
  const yearOf = new Map(extra.filter((r) => r.predicate === 'founded_year').map((r) => [r.root, r.value]));

  const outEdges = datedEdges(out);
  const outAttrs = out.filter((r) => r.other === null && !EXCLUDED.has(r.predicate));
  const inEdges = datedEdges(inc);
  const foundedByYear = undatedFounded.filter((r) => yearAt(yearOf.get(r.other!)) !== null);
  const undatedFounders = kind === 'company' ? founderRows.filter((r) => !r.valid_from && !r.valid_to) : [];
  // One toFacts per direction (each is two queries; a transaction runs them one at a time).
  const outGroups = [outEdges, outAttrs, posted, foundedByYear];
  const inGroups = [inEdges, undatedFounders];
  const [outAll, inAll] = await Promise.all([toFacts(tx, outGroups.flat(), 'out'), toFacts(tx, inGroups.flat(), 'in')]);
  const [outEdgeFacts = [], outAttrFacts = [], postedFacts = [], foundedFacts = []] = split(outAll, outGroups);
  const [inFacts = [], founderFacts = []] = split(inAll, inGroups);

  const events = [
    ...edgeEvents(outEdgeFacts, outEdges, 'out', label),
    ...attributeEvents(outAttrFacts, outAttrs, vocab, founderFacts.map((f) => f.subject)),
    ...edgeEvents(inFacts, inEdges, 'in', label),
    ...postedByFounders(postedFacts, posted),
    ...foundedFacts.flatMap((fact, i): TimelineEvent[] => {
      const at = yearAt(yearOf.get(foundedByYear[i]!.other!));
      if (!at || !fact.object) return [];
      return [
        {
          key: `out|founded|${fact.object.id}|${fact.layer}|year`,
          at,
          kind: 'founded',
          title: `Founded ${fact.object.name ?? fact.object.id}`,
          predicate: 'founded',
          other: fact.object,
          fact,
          dir: 'out',
          precision: 'year',
          headline: true,
        },
      ];
    }),
  ];
  const f = first[0];
  if (f) {
    const at = iso(f.at);
    events.push({
      key: 'first_seen',
      at,
      kind: 'first_seen',
      title: 'First seen in Rebox',
      predicate: f.predicate,
      other: null,
      fact: null,
      dir: null,
      precision: 'time',
      headline: false,
      layer: f.layer_id,
    });
  }
  return clusterMilestones(events).sort(compareEvents);
}

// ── milestone clustering (S18) ────────────────────────────────────────────

/** Members of a cluster are dated within this many days of its first (earliest) member. */
export const CLUSTER_DAYS = 45;
/** Token Jaccard at or above this makes two milestone titles "similar". */
export const CLUSTER_JACCARD = 0.3;
/** Titles longer than this are only picked when no shorter one qualifies. */
const MAX_TITLE = 120;
const DAY_MS = 86_400_000;

const STOPWORDS: ReadonlySet<string> = new Set(
  (
    'a an the and or of for to in on at by with from as is are was were be been being its it this that these ' +
    'their our we us his her has have had into after over about than via up out its new more first'
  ).split(' '),
);

const MULT: Record<string, number> = {
  k: 1e3, thousand: 1e3,
  m: 1e6, mm: 1e6, mn: 1e6, million: 1e6, mio: 1e6,
  b: 1e9, bn: 1e9, billion: 1e9,
}; // prettier-ignore

// "$9.7M", "$ 9.7 million", "€5m", "$3,499", "9.7 million", "1.2bn" (no currency: a multiplier is required).
const AMOUNT_RE =
  /([$€£¥])\s?(\d+(?:[.,]\d+)*)(?:\s?(k|mm|mn|m|bn|b|thousand|million|mio|billion)\b)?|(?<![\w.$€£¥])(\d+(?:[.,]\d+)?)(?:\s?(thousand|million|mio|billion)\b|(mn|m|bn|b)\b)/gi;

function parseNumber(raw: string): number {
  // "3,499" / "1,250,000": thousands separators; "9,7": a decimal comma.
  if (/^\d{1,3}(,\d{3})+(\.\d+)?$/.test(raw)) return Number(raw.replace(/,/g, ''));
  return Number(raw.replace(',', '.'));
}

/** Money amounts in a title, normalized to whole units: "$9.7M" and "9.7 million" -> "amt:9700000". */
export function amountTokens(title: string): string[] {
  const out: string[] = [];
  for (const m of title.matchAll(AMOUNT_RE)) {
    const num = parseNumber(m[2] ?? m[4] ?? '');
    const mult = MULT[(m[3] ?? m[5] ?? m[6] ?? '').toLowerCase()] ?? 1;
    if (Number.isFinite(num) && num > 0) out.push(`amt:${Math.round(num * mult)}`);
  }
  return out;
}

/** Lowercased word tokens without stopwords (light stemming: raises/raised/raising -> rais), plus amounts. */
export function titleTokens(title: string): Set<string> {
  const amounts = amountTokens(title);
  const words = title
    .replace(AMOUNT_RE, ' ')
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((w) => w.length > 1 && !STOPWORDS.has(w))
    .map((w) => (w.length > 4 ? w.replace(/(ing|ed|es|s)$/, '') : w));
  return new Set([...words, ...amounts]);
}

export function jaccard(a: ReadonlySet<string>, b: ReadonlySet<string>): number {
  if (!a.size && !b.size) return 0;
  let both = 0;
  for (const x of a) if (b.has(x)) both++;
  return both / (a.size + b.size - both);
}

/** Same milestone, by title: token Jaccard >= 0.3, or a money amount in common. */
export function similarTitles(a: string, b: string): boolean {
  const ta = titleTokens(a);
  const tb = titleTokens(b);
  for (const x of ta) if (x.startsWith('amt:') && tb.has(x)) return true;
  return jaccard(ta, tb) >= CLUSTER_JACCARD;
}

const TRUST: Record<string, number> = { connector: 0.8, spider: 0.8, parser: 0.6, enricher: 0.5, external: 0.3 };

/** Default trust of the fact's worker (CONTRACTS §12), from its id `<name>-<kind>-<digits>`; else by layer. */
function trustOf(f: Fact | null): number {
  const kind = f?.worker?.split('-').at(-2);
  if (kind && kind in TRUST) return TRUST[kind]!;
  return f?.layer.endsWith('_ai') ? TRUST.parser! : TRUST.connector!;
}

function milestoneUrl(e: TimelineEvent): string | null {
  const url = milestoneValue(e.fact?.value).url ?? e.fact?.evidence?.[0]?.url ?? null;
  return url && /^https?:\/\//i.test(url) ? url : null;
}

const milestoneKind = (e: TimelineEvent) => (milestoneValue(e.fact?.value).kind ?? 'other').toLowerCase();

/** Best title of a cluster: most trusted source, then one with an amount, then the longest <= 120 chars. */
function pickTitle(members: TimelineEvent[]): TimelineEvent {
  const rank = (e: TimelineEvent) => [
    -trustOf(e.fact),
    amountTokens(e.title).length ? 0 : 1,
    e.title.length <= MAX_TITLE ? 0 : 1,
    e.title.length <= MAX_TITLE ? -e.title.length : e.title.length,
  ];
  let best = members[0]!;
  let bestRank = rank(best);
  for (const e of members.slice(1)) {
    const r = rank(e);
    const i = r.findIndex((x, j) => x !== bestRank[j]);
    if (i >= 0 && r[i]! < bestRank[i]!) {
      best = e;
      bestRank = r;
    }
  }
  return best;
}

function sourcesOf(members: TimelineEvent[]): { sources: EventSource[]; support: number } {
  const sources: EventSource[] = [];
  const seen = new Set<string>();
  let urlless = 0;
  for (const e of members) {
    const url = milestoneUrl(e);
    if (!url) {
      urlless++;
      continue;
    }
    if (seen.has(url)) continue;
    seen.add(url);
    sources.push({ url, title: e.title, layer: e.fact?.layer ?? '' });
  }
  return { sources, support: Math.max(1, sources.length + urlless) };
}

/**
 * Groups `milestone` events of the same milestone kind (launch, funding, hire, ...) dated within
 * CLUSTER_DAYS of the cluster's first event and with similar titles (`similarTitles` with any
 * member) into one event: date and key from the earliest member (`milestone-cluster|<its key>`),
 * the title of `pickTitle`, `sources` from every member (one per url) and `support` = distinct
 * urls. A milestone without a match stays as it is (with `sources` and `support: 1`). Other
 * events are untouched. Deterministic: members are visited oldest first, ties by key, and join
 * the first cluster that fits.
 */
export function clusterMilestones(events: TimelineEvent[]): TimelineEvent[] {
  const rest: TimelineEvent[] = [];
  const ms: TimelineEvent[] = [];
  for (const e of events) (e.kind === 'milestone' && e.predicate === 'milestone' ? ms : rest).push(e);
  if (!ms.length) return events;
  ms.sort((a, b) => (a.at !== b.at ? (a.at < b.at ? -1 : 1) : a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  const clusters: { kind: string; start: number; members: TimelineEvent[] }[] = [];
  for (const e of ms) {
    const kind = milestoneKind(e);
    const t = Date.parse(e.at);
    const hit = clusters.find(
      (c) => c.kind === kind && t - c.start <= CLUSTER_DAYS * DAY_MS && c.members.some((m) => similarTitles(m.title, e.title)),
    );
    if (hit) hit.members.push(e);
    else clusters.push({ kind, start: t, members: [e] });
  }
  for (const c of clusters) {
    const first = c.members[0]!;
    const { sources, support } = sourcesOf(c.members);
    if (c.members.length === 1) {
      rest.push({ ...first, sources, support });
      continue;
    }
    const best = pickTitle(c.members);
    rest.push({
      ...first,
      key: `milestone-cluster|${first.key}`,
      title: best.title,
      fact: best.fact,
      headline: c.members.some((m) => m.headline),
      sources,
      support,
    });
  }
  return rest;
}

/** Cuts `all` back into consecutive slices the size of each group. */
function split<T>(all: T[], groups: unknown[][]): T[][] {
  let i = 0;
  return groups.map((g) => {
    const part = all.slice(i, i + g.length);
    i += g.length;
    return part;
  });
}

function postedByFounders(facts: Fact[], rows: FoldedRow[]): TimelineEvent[] {
  return rows.flatMap((r, i) => {
    const fact = facts[i]!;
    const at = r.valid_from ?? r.valid_to;
    if (!at || !fact.object) return [];
    const atIso = iso(at);
    const who = fact.subject.name ?? fact.subject.id;
    return [
      {
        key: `founder|posted|${r.root}|${r.other}|${r.layer_id}`,
        at: atIso,
        kind: 'posted' as const,
        title: `${who} posted ${fact.object.name ?? fact.object.id}`,
        predicate: 'posted',
        other: fact.object,
        fact,
        dir: 'out' as const,
        precision: precisionOf(atIso, 'posted'),
        headline: true,
      },
    ];
  });
}
