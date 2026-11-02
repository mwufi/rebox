/** Neighborhood graph model: nodes and links from entity views, with hub capping. Pure. */
import type { EntityRef, EntityView } from '../api';
import { edgeCounts } from './facts';

export interface GNode {
  id: string;
  kind: string;
  name: string | null;
  avatar?: string;
  handle?: 'hn' | 'x';
  hop: 0 | 1 | 2;
  /** "+N more" placeholder for a capped predicate group. */
  more?: { count: number; predicate: string; dir: 'out' | 'in'; owner: string };
  x?: number;
  y?: number;
  fx?: number | null;
  fy?: number | null;
}

export interface GLink {
  source: string;
  target: string;
  predicate: string;
  hop: 1 | 2;
}

export interface Graph {
  nodes: GNode[];
  links: GLink[];
  degree: number;
}

/** Per predicate+direction, at most this many neighbors (hubs collapse to "+N more"). */
export const PER_GROUP_CAP = 12;
/** 2-hop neighbors per 1-hop node. */
export const HOP2_CAP = 6;
/** 2-hop is offered only when the center has at most this many neighbors. */
export const HOP2_MAX_DEGREE = 14;

interface Edge {
  other: EntityRef;
  predicate: string;
  dir: 'out' | 'in';
}

export function edgesOf(view: Pick<EntityView, 'facts' | 'incoming'>): Edge[] {
  const out: Edge[] = view.facts.filter((f) => f.object).map((f) => ({ other: f.object!, predicate: f.predicate, dir: 'out' }));
  const inc: Edge[] = view.incoming.map((f) => ({ other: f.subject, predicate: f.predicate, dir: 'in' }));
  return [...out, ...inc];
}

type CountedView = Pick<EntityView, 'facts' | 'incoming' | 'facts_counts' | 'incoming_counts'>;

/**
 * Unique neighbor count. The view carries only the first edges of each predicate (S13); edges it
 * did not carry are added from its counts, so a hub is never mistaken for a small neighborhood.
 */
export function degreeOf(view: CountedView): number {
  const edges = edgesOf(view);
  const counts = edgeCounts(view);
  const loaded = new Map<string, Set<string>>();
  for (const e of edges) {
    const k = `${e.dir}:${e.predicate}`;
    loaded.set(k, (loaded.get(k) ?? new Set()).add(e.other.id));
  }
  let missing = 0;
  for (const [dir, byPred] of [['out', counts.out], ['in', counts.in]] as const) {
    for (const [p, n] of Object.entries(byPred)) missing += Math.max(0, n - (loaded.get(`${dir}:${p}`)?.size ?? 0));
  }
  return new Set(edges.map((e) => e.other.id)).size + missing;
}

function addCapped(
  g: { nodes: Map<string, GNode>; links: GLink[]; linkKeys: Set<string> },
  owner: string,
  edges: Edge[],
  hop: 1 | 2,
  cap: number,
  /** Totals per `${dir}:${predicate}` when the view carried only some edges (S13 counts). */
  totals?: Map<string, number>,
) {
  const groups = new Map<string, Edge[]>();
  for (const e of edges) {
    const k = `${e.dir}:${e.predicate}`;
    const list = groups.get(k);
    if (list) list.push(e);
    else groups.set(k, [e]);
  }
  for (const [k, list] of groups) {
    const shown = list.slice(0, cap);
    for (const e of shown) {
      if (!g.nodes.has(e.other.id)) g.nodes.set(e.other.id, { ...e.other, hop });
      const [s, t] = e.dir === 'out' ? [owner, e.other.id] : [e.other.id, owner];
      const key = `${s}|${t}|${e.predicate}`;
      if (!g.linkKeys.has(key)) {
        g.linkKeys.add(key);
        g.links.push({ source: s, target: t, predicate: e.predicate, hop });
      }
    }
    const rest = Math.max(list.length, totals?.get(k) ?? 0) - shown.length;
    if (rest > 0) {
      const id = `more:${owner}:${k}`;
      const [dir, predicate] = k.split(':') as ['out' | 'in', string];
      g.nodes.set(id, { id, kind: 'more', name: `+${rest} more`, hop, more: { count: rest, predicate, dir, owner } });
      g.links.push({ source: dir === 'out' ? owner : id, target: dir === 'out' ? id : owner, predicate, hop });
    }
  }
}

/** Builds the graph for `center`, optionally adding 2-hop neighbors from `second` views. */
export function buildGraph(center: EntityView, second: EntityView[] = [], caps = { perGroup: PER_GROUP_CAP, hop2: HOP2_CAP }): Graph {
  const g = { nodes: new Map<string, GNode>(), links: [] as GLink[], linkKeys: new Set<string>() };
  const centerNode: GNode = { id: center.id, kind: center.kind, name: center.name, hop: 0 };
  const avatar = center.facts.find((f) => f.predicate === 'avatar')?.value;
  if (typeof avatar === 'string' && /^https?:\/\//i.test(avatar)) centerNode.avatar = avatar;
  g.nodes.set(center.id, centerNode);
  // Dedupe edges to the same neighbor+predicate+dir (several date ranges).
  const seen = new Set<string>();
  const first = edgesOf(center).filter((e) => {
    const k = `${e.dir}|${e.predicate}|${e.other.id}`;
    if (seen.has(k) || e.other.id === center.id) return false;
    seen.add(k);
    return true;
  });
  const counts = edgeCounts(center);
  const totals = new Map<string, number>([
    ...Object.entries(counts.out).map(([p, n]) => [`out:${p}`, n] as [string, number]),
    ...Object.entries(counts.in).map(([p, n]) => [`in:${p}`, n] as [string, number]),
  ]);
  addCapped(g, center.id, first, 1, caps.perGroup, totals);

  for (const v of second) {
    if (!g.nodes.has(v.id)) continue;
    const s2 = new Set<string>();
    const edges = edgesOf(v).filter((e) => {
      const k = `${e.dir}|${e.predicate}|${e.other.id}`;
      if (s2.has(k)) return false;
      s2.add(k);
      return true;
    });
    // Links among already-present nodes always show; new nodes are capped per neighbor.
    const known = edges.filter((e) => g.nodes.has(e.other.id));
    const fresh = edges.filter((e) => !g.nodes.has(e.other.id)).slice(0, caps.hop2);
    for (const e of known) {
      const [s, t] = e.dir === 'out' ? [v.id, e.other.id] : [e.other.id, v.id];
      const key = `${s}|${t}|${e.predicate}`;
      if (!g.linkKeys.has(key) && s !== t) {
        g.linkKeys.add(key);
        g.links.push({ source: s, target: t, predicate: e.predicate, hop: 2 });
      }
    }
    for (const e of fresh) {
      g.nodes.set(e.other.id, { ...e.other, hop: 2 });
      const [s, t] = e.dir === 'out' ? [v.id, e.other.id] : [e.other.id, v.id];
      g.linkKeys.add(`${s}|${t}|${e.predicate}`);
      g.links.push({ source: s, target: t, predicate: e.predicate, hop: 2 });
    }
  }
  return { nodes: [...g.nodes.values()], links: g.links, degree: new Set(first.map((e) => e.other.id)).size };
}
