/** Grouping of an entity view's facts for the entity page. Pure; tested in test/facts.test.ts. */
import type { EntityView, Fact, Predicate } from '../api';

/** Attributes shown in the header strip, in this order (when present). */
export const KEY_ATTRS = ['description', 'headline', 'website', 'domain', 'yc_batch', 'founded_year'] as const;

/** Never shown as rows: `name` is the title. */
const HIDDEN = new Set(['name']);

export interface FactGroup {
  predicate: string;
  label: string;
  facts: Fact[];
  /** Edges in the group, including any the view did not carry (S13 counts); else facts.length. */
  total: number;
}

export interface GroupedFacts {
  /** Key attributes for the header, in KEY_ATTRS order. */
  key: FactGroup[];
  /** Remaining attributes (identity handles, counts, location, ...), by label. */
  attributes: FactGroup[];
  /** Outgoing edges grouped by predicate. */
  outgoing: FactGroup[];
  /** Incoming edges grouped by predicate. */
  incoming: FactGroup[];
  /** Total number of facts (excluding the name), counting edges the view did not carry. */
  total: number;
}

export type PredicateIndex = ReadonlyMap<string, Predicate>;

export function labelOf(predicate: string, preds?: PredicateIndex): string {
  const l = preds?.get(predicate)?.meta.label;
  if (l) return l;
  const s = predicate.replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Incoming labels read from the other side: "founded" on a company reads "Founded by". */
const INCOMING_LABELS: Record<string, string> = {
  worked_at: 'People',
  founded: 'Founded by',
  ceo_of: 'CEO',
  invested_in: 'Investors',
  posted: 'Posted by',
  launched: 'Launched by',
  based_in: 'Based here',
  attended: 'Attendees',
  knows: 'Known by',
  emailed: 'Emailed by',
};

export const incomingLabel = (predicate: string, preds?: PredicateIndex) =>
  INCOMING_LABELS[predicate] ?? `${labelOf(predicate, preds)} (incoming)`;

/** Edge facts sort current first (no end date), then by most recent start, then by name. */
export function compareEdges(a: Fact, b: Fact, side: 'object' | 'subject'): number {
  const openA = a.valid_to === null ? 0 : 1;
  const openB = b.valid_to === null ? 0 : 1;
  if (openA !== openB) return openA - openB;
  const fa = a.valid_from ?? '';
  const fb = b.valid_from ?? '';
  if (fa !== fb) return fa < fb ? 1 : -1;
  const na = (a[side]?.name ?? a[side]?.id ?? '').toLowerCase();
  const nb = (b[side]?.name ?? b[side]?.id ?? '').toLowerCase();
  return na < nb ? -1 : na > nb ? 1 : 0;
}

const isEdgeFact = (f: Fact) => f.object !== null;

function group(facts: Fact[], label: (p: string) => string, counts?: Record<string, number>): FactGroup[] {
  const by = new Map<string, Fact[]>();
  for (const f of facts) {
    const list = by.get(f.predicate);
    if (list) list.push(f);
    else by.set(f.predicate, [f]);
  }
  return [...by.entries()].map(([predicate, list]) => ({
    predicate,
    label: label(predicate),
    facts: list,
    total: Math.max(list.length, counts?.[predicate] ?? 0),
  }));
}

/**
 * Edge counts per direction and predicate. The entity view carries only the first edges of each
 * predicate plus `facts_counts` / `incoming_counts` (S13); views without counts carry every edge.
 */
export function edgeCounts(view: Pick<EntityView, 'facts' | 'incoming' | 'facts_counts' | 'incoming_counts'>) {
  const tally = (facts: Fact[], counts?: Record<string, number>) => {
    const m: Record<string, number> = {};
    for (const f of facts) m[f.predicate] = (m[f.predicate] ?? 0) + 1;
    for (const [p, n] of Object.entries(counts ?? {})) m[p] = Math.max(m[p] ?? 0, n);
    return m;
  };
  return { out: tally(view.facts.filter(isEdgeFact), view.facts_counts), in: tally(view.incoming, view.incoming_counts) };
}

/** Total edges in both directions, including those the view did not carry. */
export function edgeTotal(view: Pick<EntityView, 'facts' | 'incoming' | 'facts_counts' | 'incoming_counts'>): number {
  const c = edgeCounts(view);
  return [...Object.values(c.out), ...Object.values(c.in)].reduce((a, b) => a + b, 0);
}


/** Larger edge groups first (they say most about the entity), then by label. */
const bySizeThenLabel = (a: FactGroup, b: FactGroup) => b.total - a.total || a.label.localeCompare(b.label);

export function groupFacts(
  view: Pick<EntityView, 'facts' | 'incoming' | 'facts_counts' | 'incoming_counts'>,
  preds?: PredicateIndex,
): GroupedFacts {
  const visible = view.facts.filter((f) => !HIDDEN.has(f.predicate));
  const attrs = visible.filter((f) => !isEdgeFact(f));
  const edges = visible.filter(isEdgeFact);
  const label = (p: string) => labelOf(p, preds);

  const keySet = new Set<string>(KEY_ATTRS);
  const key = group(
    attrs.filter((f) => keySet.has(f.predicate)),
    label,
  ).sort((a, b) => KEY_ATTRS.indexOf(a.predicate as never) - KEY_ATTRS.indexOf(b.predicate as never));
  const attributes = group(
    attrs.filter((f) => !keySet.has(f.predicate)),
    label,
  ).sort((a, b) => a.label.localeCompare(b.label));

  const outgoing = group(edges, label, view.facts_counts)
    .map((g) => ({ ...g, facts: [...g.facts].sort((a, b) => compareEdges(a, b, 'object')) }))
    .sort(bySizeThenLabel);
  const incoming = group(view.incoming, (p) => incomingLabel(p, preds), view.incoming_counts)
    .map((g) => ({ ...g, facts: [...g.facts].sort((a, b) => compareEdges(a, b, 'subject')) }))
    .sort(bySizeThenLabel);

  const sum = (gs: FactGroup[]) => gs.reduce((n, g) => n + g.total, 0);
  return { key, attributes, outgoing, incoming, total: visible.length - edges.length + sum(outgoing) + sum(incoming) };
}

/** Distinct sources contributing to a view (for the provenance summary). */
export function sourcesOf(view: Pick<EntityView, 'facts' | 'incoming'>): { source: string; layer: string; n: number }[] {
  const by = new Map<string, { source: string; layer: string; n: number }>();
  for (const f of [...view.facts, ...view.incoming]) {
    const cur = by.get(f.layer);
    if (cur) cur.n += 1;
    else by.set(f.layer, { source: f.source, layer: f.layer, n: 1 });
  }
  return [...by.values()].sort((a, b) => b.n - a.n || a.layer.localeCompare(b.layer));
}
