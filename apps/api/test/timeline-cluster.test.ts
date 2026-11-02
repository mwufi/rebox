/**
 * Milestone clustering on timelines (S18): the same milestone reported by many outlets is one
 * event with its sources. Known-shaped fixtures, inserted as the owner like a parser's claims.
 */
import { beforeAll, describe, expect, it } from 'vitest';
import { amountTokens, clusterMilestones, similarTitles, titleTokens, type TimelineEvent } from '../src/timeline.js';
import { setupApp } from './helpers.js';

const t = setupApp({ env: 'dev' });
const P = 'public/s18_ai';
const H = 'public/s18hn';
const known = 'rbx:company:s18cl-known';
const other = 'rbx:company:s18cl-other';

// Ten outlets report the same $9.7M raise over three weeks, with different titles.
const RAISE = [
  ['2025-12-18', 'Known raised $9.7M from Forerunner, NFX, Pear VC', 'techcrunch.com/known'],
  ['2025-12-18', 'Raised $9.7M to help people find love', 'linkedin.com/posts/celeste'],
  ['2025-12-19', 'Known raised $9.7M from Forerunner, NFX, Pear VC, Coelius', 'whistlebuzz.com/known'],
  ['2025-12-21', 'Known raised $9.7M in funding', 'onlinepersonalswatch.com/known'],
  ['2025-12-23', 'Known raises 9.7 million dollars for voice AI dating', 'example.org/a'],
  ['2025-12-26', 'Voice dating app Known gets $9.7 million', 'example.org/b'],
  ['2025-12-29', 'Known raised $9.7M led by Forerunner Ventures', 'datingindustryinsights.com/known'],
  ['2026-01-02', 'Known bags $9.7M seed', 'example.org/c'],
  ['2026-01-05', 'Known raised $9.7M led by Forerunner Ventures to reinvent dating with voice AI matchmaking', 'example.org/d'],
  ['2026-01-07', 'Known raised $9.7M led by Forerunner Ventures', 'vctavern.com/known'],
] as const;

async function claim(subject: string, value: unknown, from: string, ref: string, layer = P) {
  await t.owner`
    insert into claim (layer_id, subject, predicate, value, valid_from, source_ref)
    values (${layer}, ${subject}, 'milestone', ${t.owner.json(value as never)}, ${from}, ${ref})`;
}

beforeAll(async () => {
  await t.owner`
    insert into layer (id, scope, owner, source) values (${P}, 'public', null, 's18_ai'), (${H}, 'public', null, 's18hn')
    on conflict do nothing`;
  for (const [id, name] of [[known, 'Known'], [other, 'Other Co']] as const) {
    await t.owner`insert into entity (id, kind, home_layer) values (${id}, 'company', ${P}) on conflict do nothing`;
    await t.owner`insert into claim (layer_id, subject, predicate, value, source_ref)
                  values (${P}, ${id}, 'name', ${t.owner.json(name)}, ${`s18cl:${id}:name`}) on conflict do nothing`;
  }
  for (const [i, [at, title, url]] of RAISE.entries()) {
    await claim(known, { title, kind: 'funding', url: `https://${url}` }, at, `s18cl:raise:${i}`);
  }
  // The same article twice (two source_refs, one url): one source.
  await claim(known, { title: 'Known raised $9.7M in funding!', kind: 'funding', url: 'https://onlinepersonalswatch.com/known' }, '2025-12-21', 's18cl:raise:dup');
  // A launch on the same day as the first funding report: its own event.
  await claim(known, { title: 'Known launching product with planned dates in SF', kind: 'launch', url: 'https://linkedin.com/posts/celeste' }, '2025-12-18', 's18cl:launch');
  // Another funding six months later (same wording, different round): its own event.
  await claim(known, { title: 'Known raised $25M Series A led by Forerunner Ventures', kind: 'funding', url: 'https://techcrunch.com/known-a' }, '2026-06-20', 's18cl:seriesa');
  // A single-source milestone looks as before (no cluster key).
  await claim(known, { title: 'Known wins Best Dating App award', kind: 'award', url: 'https://awards.example/known' }, '2026-03-01', 's18cl:award');
  // 50 days after the first report: outside the window (the cluster is anchored on its first event).
  await claim(known, { title: 'Known raised $9.7M last year', kind: 'funding', url: 'https://example.org/late' }, '2026-02-06', 's18cl:late');
});

type Src = { url: string; title: string; layer: string };
type Ev = { at: string; kind: string; title: string; key: string; support?: number; sources?: Src[]; headline: boolean; fact: { value: { url: string } } | null };
const timeline = async (eid: string, qs = '') => {
  const r = await t.get(`/v0/entities/${encodeURIComponent(eid)}/timeline${qs}`);
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body as { id: string; events: Ev[]; next: string | null };
};

describe('milestone clusters on GET /v0/entities/:id/timeline', () => {
  it('ten reports of one raise over three weeks are one event with ten sources', async () => {
    const ms = (await timeline(known)).events.filter((e) => e.kind === 'milestone');
    expect(ms.map((e) => `${e.at.slice(0, 10)} ${e.title} (${e.support})`)).toEqual([
      '2026-06-20 Known raised $25M Series A led by Forerunner Ventures (1)',
      '2026-03-01 Known wins Best Dating App award (1)',
      '2026-02-06 Known raised $9.7M last year (1)',
      '2025-12-18 Known launching product with planned dates in SF (1)',
      // the earliest date; the longest title with an amount, <= 120 chars (same-day ties by key)
      '2025-12-18 Known raised $9.7M led by Forerunner Ventures to reinvent dating with voice AI matchmaking (10)',
    ]);
    const raise = ms.find((e) => e.support === 10)!;
    expect(raise.key.startsWith('milestone-cluster|attr|milestone|')).toBe(true);
    expect(raise.headline).toBe(true);
    expect(raise.sources).toHaveLength(10);
    expect(new Set(raise.sources!.map((s) => s.url)).size).toBe(10);
    // oldest first (two reports on Dec 18, by key), each with its own title and layer
    expect(raise.sources!.slice(0, 2).map((s) => s.url).sort()).toEqual(['https://linkedin.com/posts/celeste', 'https://techcrunch.com/known']);
    expect(raise.sources!.at(-1)).toEqual({ url: 'https://vctavern.com/known', title: 'Known raised $9.7M led by Forerunner Ventures', layer: P });
    expect(raise.fact!.value.url).toBe('https://example.org/d'); // the fact behind the chosen title
    // singles keep their key and carry their one source
    const award = ms.find((e) => e.title.includes('award'))!;
    expect(award.key.startsWith('attr|milestone|')).toBe(true);
    expect(award.sources).toEqual([{ url: 'https://awards.example/known', title: 'Known wins Best Dating App award', layer: P }]);
  });

  it('a launch and a funding on the same day, and two fundings 6 months apart, stay separate', async () => {
    const ms = (await timeline(known)).events.filter((e) => e.kind === 'milestone');
    expect(ms.filter((e) => e.at.startsWith('2025-12-18'))).toHaveLength(2);
    expect(ms.filter((e) => e.title.includes('Series A'))).toHaveLength(1);
  });

  it('paginates clustered events without gaps or repeats', async () => {
    const all = (await timeline(known)).events.map((e) => e.key);
    expect(new Set(all).size).toBe(all.length);
    const seen: string[] = [];
    let cursor: string | null = null;
    do {
      const page: { events: Ev[]; next: string | null } = await timeline(known, `?limit=2${cursor ? `&cursor=${cursor}` : ''}`);
      seen.push(...page.events.map((e) => e.key));
      cursor = page.next;
    } while (cursor);
    expect(seen).toEqual(all);
  });

  it('other entities and non-milestone events are untouched', async () => {
    const body = await timeline(other);
    expect(body.events.map((e) => e.kind)).toEqual(['first_seen']);
  });
});

// ── pure ──

let n = 0;
function ev(at: string, title: string, kind = 'funding', opts: { url?: string | null; layer?: string; worker?: string | null } = {}): TimelineEvent {
  const url = opts.url === undefined ? `https://news.example/${++n}` : opts.url;
  const layer = opts.layer ?? P;
  return {
    key: `attr|milestone|${layer}|${title}|${n}`,
    at: `${at}T00:00:00.000Z`,
    kind: 'milestone',
    title,
    predicate: 'milestone',
    other: null,
    fact: {
      predicate: 'milestone',
      value: { title, kind, ...(url ? { url } : {}) },
      object: null,
      subject: { id: known, kind: 'company', name: 'Known' },
      valid_from: `${at}T00:00:00.000Z`,
      valid_to: null,
      layer,
      source: layer.split('/')[1]!,
      source_ref: `r${n}`,
      updated_at: `${at}T00:00:00.000Z`,
      worker: opts.worker === undefined ? 'exa-parser-123456' : opts.worker,
      evidence: null,
      support: 1,
    },
    dir: 'out',
    precision: 'day',
    headline: true,
  };
}

describe('clusterMilestones', () => {
  it('amounts normalize across spellings', () => {
    expect(amountTokens('raised $9.7M')).toEqual(['amt:9700000']);
    expect(amountTokens('gets 9.7 million')).toEqual(['amt:9700000']);
    expect(amountTokens('€5m seed; $1.2bn valuation; $3,499 box')).toEqual(['amt:5000000', 'amt:1200000000', 'amt:3499']);
    expect(amountTokens('In 2026, 80% of beta users')).toEqual([]);
    expect(titleTokens('Known raises $9.7M')).toEqual(new Set(['known', 'rais', 'amt:9700000']));
    expect(similarTitles('Known bags $9.7M seed', 'Voice dating app gets 9.7 million')).toBe(true);
    expect(similarTitles('Known launched beta in SF', 'Known launches its beta to SF users')).toBe(true);
    expect(similarTitles('Known launched beta in SF', 'Ditto hires a party planner')).toBe(false);
  });

  it('is deterministic and order-independent; non-milestones pass through', () => {
    const evs = [
      ev('2026-01-01', 'Acme raised $5M seed'),
      ev('2026-01-10', 'Acme raises $5 million'),
      ev('2026-01-01', 'Acme launched', 'launch'),
      { ...ev('2026-01-02', 'Joined Acme'), kind: 'joined' as const, predicate: 'worked_at' },
    ];
    const a = clusterMilestones(evs);
    const b = clusterMilestones([...evs].reverse());
    const sort = (x: TimelineEvent[]) => x.map((e) => `${e.key}|${e.title}|${e.support ?? '-'}`).sort();
    expect(sort(a)).toEqual(sort(b));
    expect(a).toHaveLength(3);
    expect(a.find((e) => e.kind === 'joined')).toBe(evs[3]);
  });

  it('title: most trusted source, then one with an amount, then the longest <= 120 chars', () => {
    const long = `Acme raised $5M ${'x'.repeat(130)}`;
    const [c1] = clusterMilestones([ev('2026-01-01', 'Acme raised $5M'), ev('2026-01-02', 'Acme raised $5M from Lux and NFX'), ev('2026-01-03', long)]);
    expect(c1!.title).toBe('Acme raised $5M from Lux and NFX');
    const [c2] = clusterMilestones([
      ev('2026-01-01', 'Acme raised a seed round led by Lux Capital'),
      ev('2026-01-02', 'Acme raised a seed round'),
    ]);
    expect(c2!.title).toBe('Acme raised a seed round led by Lux Capital');
    // a connector-sourced milestone outranks parsed articles
    const [c3] = clusterMilestones([
      ev('2026-01-01', 'Acme raised $5M seed led by Lux Capital and friends'),
      ev('2026-01-02', 'Acme raises seed', 'funding', { layer: 'public/hn', worker: 'hn-connector-000001' }),
    ]);
    expect(c3!.title).toBe('Acme raises seed');
  });

  it('support counts distinct urls; url-less members count once each', () => {
    const [c] = clusterMilestones([
      ev('2026-01-01', 'Acme raised $5M', 'funding', { url: 'https://a.example/1' }),
      ev('2026-01-02', 'Acme raised $5M seed', 'funding', { url: 'https://a.example/1' }),
      ev('2026-01-03', 'Acme raises $5M', 'funding', { url: null }),
    ]);
    expect(c!.support).toBe(2);
    expect(c!.sources!.map((s) => s.url)).toEqual(['https://a.example/1']);
  });
});
