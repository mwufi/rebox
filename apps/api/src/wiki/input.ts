/**
 * Wiki input: everything the model may use, gathered under a PUBLIC-ONLY view (CONTRACTS §14:
 * "from the entity's public facts"), and the prompt built from it.
 *
 * The input is a numbered source list plus fact lines that cite those numbers, so the model can
 * only cite what we give it, and every footnote resolves to a real URL.
 */
import type { Tx } from '../db.js';
import type { EntityRef } from '../entities.js';
import { foldFacts } from '../fold.js';
import { buildTimeline, type TimelineEvent } from '../timeline.js';
import { toFacts, type Fact } from '../views.js';
import type { Vocab } from '../vocab.js';
import type { WikiPrompt } from './model.js';

export interface WikiSource {
  n: number;
  url: string;
  label: string;
}

export interface WikiInput {
  entity: EntityRef;
  /** Folded public facts on the entity (outgoing, incl. attributes). */
  facts: Fact[];
  /** Folded public incoming edges (capped). */
  incoming: Fact[];
  timeline: TimelineEvent[];
  /** 1-hop neighbors with their headline/description (one line each). */
  neighbors: { ref: EntityRef; line: string | null; roles: string[] }[];
  sources: WikiSource[];
  /** Staleness baseline (set by the wiki service from the change feed). */
  input_seq: number;
}

const MAX_INCOMING = 150;
const MAX_NEIGHBORS = 60;
const MAX_TIMELINE = 60;

const HN_STORY = /^hn:story:(\d+):/;
const HN_USER = /^hn:user:([^:]+):/;
const WIKIDATA = /^wikidata:(Q\d+)(?::|$)/;
const GITHUB_USER = /^github:(?:user|org):([^:]+):/;
const X_HANDLE = /^x:([^:]+):/;

/** Best public URL for a fact: its evidence (S7, when present), else one derived from source_ref. */
export function sourceUrl(f: Pick<Fact, 'source' | 'source_ref'> & { evidence?: unknown }): { url: string; label: string } | null {
  const ev = Array.isArray(f.evidence) ? (f.evidence as { url?: unknown; author?: unknown }[]) : [];
  const first = ev.find((e) => typeof e?.url === 'string' && /^https?:\/\//.test(e.url as string));
  if (first) {
    const host = safeHost(first.url as string);
    return { url: first.url as string, label: typeof first.author === 'string' && first.author ? `${first.author} (${host})` : host };
  }
  const ref = f.source_ref;
  let m: RegExpExecArray | null;
  if ((m = HN_STORY.exec(ref))) return { url: `https://news.ycombinator.com/item?id=${m[1]}`, label: `Hacker News item ${m[1]}` };
  if ((m = HN_USER.exec(ref))) return { url: `https://news.ycombinator.com/user?id=${encodeURIComponent(m[1]!)}`, label: `Hacker News user ${m[1]}` };
  if ((m = WIKIDATA.exec(ref))) return { url: `https://www.wikidata.org/wiki/${m[1]}`, label: `Wikidata ${m[1]}` };
  if ((m = GITHUB_USER.exec(ref))) return { url: `https://github.com/${m[1]}`, label: `GitHub ${m[1]}` };
  if ((m = X_HANDLE.exec(ref))) return { url: `https://x.com/${m[1]}`, label: `X @${m[1]}` };
  return null;
}

function safeHost(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, '');
  } catch {
    return url;
  }
}

function valueText(v: unknown): string {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return v.map(valueText).join(', ');
  return JSON.stringify(v);
}

const day = (s: string | null) => (s ? s.slice(0, 10) : null);

function span(f: Pick<Fact, 'valid_from' | 'valid_to'>): string {
  const a = day(f.valid_from);
  const b = day(f.valid_to);
  if (a && b) return ` (${a} to ${b})`;
  if (a) return ` (from ${a})`;
  if (b) return ` (until ${b})`;
  return '';
}

const nameOf = (r: EntityRef) => r.name ?? r.id;

/** Gathers the wiki input for `id`. `tx` must have a public-only view. */
export async function gatherWikiInput(tx: Tx, entity: EntityRef, vocab: Vocab): Promise<WikiInput> {
  const id = entity.id;
  const [outRows, inRows, timeline] = await Promise.all([
    foldFacts(tx, { roots: [id], dir: 'out' }),
    foldFacts(tx, { roots: [id], dir: 'in', page: { after: null, limit: MAX_INCOMING } }),
    buildTimeline(tx, id, entity.kind, vocab),
  ]);
  const [facts, incoming] = await Promise.all([toFacts(tx, outRows, 'out'), toFacts(tx, inRows, 'in')]);
  const visibleFacts = facts.filter((f) => f.predicate !== 'follows');
  const visibleIncoming = incoming.filter((f) => f.predicate !== 'follows');

  // 1-hop neighbors: names, roles (predicates linking them), and a one-line description.
  const roles = new Map<string, { ref: EntityRef; roles: Set<string> }>();
  for (const f of visibleFacts) if (f.object) addRole(roles, f.object, f.predicate);
  for (const f of visibleIncoming) addRole(roles, f.subject, `${f.predicate} (incoming)`);
  const neighborIds = [...roles.keys()].slice(0, MAX_NEIGHBORS);
  const lines = neighborIds.length
    ? await foldFacts(tx, { roots: neighborIds, dir: 'out', predicates: ['description', 'headline'] })
    : [];
  const lineOf = new Map<string, string>();
  for (const r of lines) if (typeof r.value === 'string' && !lineOf.has(r.root)) lineOf.set(r.root, r.value);
  const neighbors = neighborIds.map((nid) => {
    const n = roles.get(nid)!;
    return { ref: n.ref, line: lineOf.get(nid) ?? null, roles: [...n.roles] };
  });

  // Numbered sources, in first-use order across facts, incoming, timeline.
  const sources: WikiSource[] = [];
  const byUrl = new Map<string, number>();
  const cite = (f: Fact | null) => {
    if (!f) return null;
    const s = sourceUrl(f);
    if (!s) return null;
    let n = byUrl.get(s.url);
    if (n === undefined) {
      n = sources.length + 1;
      byUrl.set(s.url, n);
      sources.push({ n, url: s.url, label: s.label });
    }
    return n;
  };
  for (const f of [...visibleFacts, ...visibleIncoming]) cite(f);
  for (const e of timeline) cite(e.fact);

  return {
    entity,
    facts: visibleFacts,
    incoming: visibleIncoming,
    timeline: timeline.slice(0, MAX_TIMELINE),
    neighbors,
    sources,
    input_seq: 0,
  };
}

function addRole(m: Map<string, { ref: EntityRef; roles: Set<string> }>, ref: EntityRef, role: string) {
  const cur = m.get(ref.id);
  if (cur) cur.roles.add(role);
  else m.set(ref.id, { ref, roles: new Set([role]) });
}

export const WIKI_SYSTEM = `You write short encyclopedia articles for Rebox, an open graph of the San Francisco startup world.

Rules:
- Use ONLY the facts in the user message. Do not add anything from your own knowledge, even if you are sure of it: no founders, dates, products, funding, locations, numbers or descriptions that are not listed.
- Every sentence that states a fact ends with a footnote marker for its source, like [^3], using only the source numbers given. Never invent a source number. If a fact lists no source, state it without a marker.
- Where the facts are silent on something a reader would expect (for example what the company does, when it was founded, or who leads it), write "Rebox doesn't know yet." for that point instead of guessing. Do not speculate or use hedges like "likely" or "probably".
- Handles such as HN usernames are not real names; refer to them as handles.
- Neutral, plain encyclopedic tone. No marketing language, no superlatives.
- Output GitHub-flavored Markdown only: a one-paragraph lead (no heading), then up to three short sections with "## " headings drawn from the facts (e.g. History, People, Launches), then nothing else. Do not write a references or footnotes section; Rebox appends it.
- 80 to 300 words.`;

/** The user message: a compact, line-oriented dossier. */
export function wikiPrompt(input: WikiInput): WikiPrompt {
  const num = new Map(input.sources.map((s) => [s.url, s.n]));
  const ref = (f: Fact | null) => {
    if (!f) return '';
    const s = sourceUrl(f);
    const n = s ? num.get(s.url) : undefined;
    return n ? ` [source ${n}]` : '';
  };
  const lines: string[] = [];
  const e = input.entity;
  lines.push(`Entity: ${nameOf(e)} (${e.kind}, id ${e.id})`);
  const nameIsHandle = input.facts.some((f) => f.predicate === 'hn_user' && f.value === e.name) ||
    input.facts.some((f) => f.predicate === 'x_handle' && typeof f.value === 'string' && f.value.toLowerCase() === (e.name ?? '').toLowerCase());
  if (nameIsHandle) lines.push(`Note: "${nameOf(e)}" is a handle, not a confirmed real name.`);

  lines.push('', 'Facts about the entity:');
  for (const f of input.facts) {
    if (f.predicate === 'name') continue;
    const what = f.object ? `${f.predicate} -> ${nameOf(f.object)} (${f.object.kind})` : `${f.predicate}: ${valueText(f.value)}`;
    const props = f.object && f.value && typeof f.value === 'object' ? ` ${JSON.stringify(f.value)}` : '';
    lines.push(`- ${what}${props}${span(f)}${ref(f)}`);
  }
  if (input.incoming.length) {
    lines.push('', 'Facts pointing at the entity:');
    for (const f of input.incoming) {
      lines.push(`- ${nameOf(f.subject)} (${f.subject.kind}) ${f.predicate} ${nameOf(e)}${span(f)}${ref(f)}`);
    }
  }
  const dated = input.timeline.filter((t) => t.kind !== 'first_seen');
  if (dated.length) {
    lines.push('', 'Timeline (newest first; "year" precision means only the year is known):');
    for (const t of dated) lines.push(`- ${t.at.slice(0, t.precision === 'year' ? 4 : 10)}: ${t.title}${ref(t.fact)}`);
  }
  if (input.neighbors.length) {
    lines.push('', 'Connected entities (context only; describe them only through the facts above):');
    for (const n of input.neighbors) {
      lines.push(`- ${nameOf(n.ref)} (${n.ref.kind}; ${n.roles.join(', ')})${n.line ? `: ${n.line}` : ''}`);
    }
  }
  lines.push('', 'Sources:');
  for (const s of input.sources) lines.push(`[source ${s.n}] ${s.label} <${s.url}>`);
  if (!input.sources.length) lines.push('(none)');
  lines.push('', `Write the article about ${nameOf(e)} now. Cite with [^N] where N is a source number above.`);
  return { system: WIKI_SYSTEM, user: lines.join('\n') };
}

/**
 * Post-processes model output: drops footnote markers that cite unknown sources, strips any
 * footnote definitions the model wrote, and appends definitions for the sources actually cited.
 */
export function finishArticle(text: string, sources: WikiSource[]): { content_md: string; cited: WikiSource[] } {
  const byN = new Map(sources.map((s) => [s.n, s]));
  let body = text
    .replace(/^```(?:markdown|md)?\s*\n([\s\S]*?)\n```\s*$/m, '$1')
    .split('\n')
    .filter((l) => !/^\[\^\d+\]:/.test(l.trim()))
    .join('\n')
    .replace(/(^|\n)#{1,6}\s*(References|Sources|Footnotes|Notes)\s*(\n[\s\S]*)?$/i, '\n')
    .trim();
  const cited: WikiSource[] = [];
  body = body.replace(/\[\^(\d+)\]/g, (_m, d: string) => {
    const s = byN.get(Number(d));
    if (!s) return '';
    if (!cited.includes(s)) cited.push(s);
    return `[^${s.n}]`;
  });
  cited.sort((a, b) => a.n - b.n);
  const defs = cited.map((s) => `[^${s.n}]: [${s.label}](${s.url})`).join('\n');
  return { content_md: defs ? `${body}\n\n${defs}\n` : `${body}\n`, cited };
}
