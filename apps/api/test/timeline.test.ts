/**
 * GET /v0/entities/:id/timeline (CONTRACTS §14). Fixtures are inserted as the owner (like a
 * connector's claims), so these tests do not depend on writer request details.
 */
import { beforeAll, describe, expect, it } from 'vitest';
import { formatSpan } from '../src/query.js';
import { describeEdge, joinNames, precisionOf } from '../src/timeline.js';
import { setupApp } from './helpers.js';

const t = setupApp({ env: 'dev' });
const alice = { 'x-rebox-viewer': 'alice' };
const bob = { 'x-rebox-viewer': 'bob' };

const P = 'public/s10tl';
const PRIV = 'user/alice/s10tl';
const id = (kind: string, s: string) => `rbx:${kind}:s10tl-${s}`;
const maya = id('person', 'maya');
const raj = id('person', 'raj');
const acme = id('company', 'acme');
const widgets = id('company', 'widgets');
const bigco = id('company', 'bigco');
const launch = id('launch', 'acme-v1');
const secretCo = id('company', 'secret');
const old = id('person', 'maya-old');

async function entity(eid: string, layer = P) {
  await t.owner`insert into entity (id, kind, home_layer) values (${eid}, ${eid.split(':')[1]!}, ${layer}) on conflict do nothing`;
}

async function claim(
  layer: string,
  subject: string,
  predicate: string,
  opts: { value?: unknown; object?: string; from?: string; to?: string; ref?: string } = {},
) {
  await t.owner`
    insert into claim (layer_id, subject, predicate, object_id, value, valid_from, valid_to, source_ref)
    values (${layer}, ${subject}, ${predicate}, ${opts.object ?? null},
            ${opts.value === undefined ? null : t.owner.json(opts.value as never)},
            ${opts.from ?? null}, ${opts.to ?? null},
            ${opts.ref ?? `s10tl:${subject}:${predicate}:${opts.object ?? JSON.stringify(opts.value ?? null)}`})`;
}

beforeAll(async () => {
  await t.owner`
    insert into layer (id, scope, owner, source) values
      (${P}, 'public', null, 's10tl'), (${PRIV}, 'user', 'alice', 's10tl')
    on conflict do nothing`;
  for (const e of [maya, raj, acme, widgets, bigco, launch, old]) await entity(e);
  await entity(secretCo, PRIV);
  // The merged-away record is named first: the fold prefers the newest name in a scope.
  const names: [string, string][] = [
    [old, 'Maya Old Record'],
    [maya, 'Maya Tl'], [raj, 'Raj Tl'], [acme, 'Acme Tl'], [widgets, 'Widgets Tl'], [bigco, 'BigCo Tl'],
    [launch, 'Acme v1'],
  ];
  for (const [e, n] of names) await claim(P, e, 'name', { value: n });
  await claim(PRIV, secretCo, 'name', { value: 'Secret Stealth Co' });

  // Maya: a job with a range, an open-ended job, a dated CEO role, a post, a founding (undated).
  await claim(P, maya, 'worked_at', { object: widgets, value: { title: 'Engineer' }, from: '2014-03-01', to: '2017-06-30' });
  await claim(P, maya, 'worked_at', { object: bigco, value: { title: 'Advisor' }, from: '2021-05-01' });
  await claim(P, maya, 'founded', { object: acme });
  await claim(P, maya, 'posted', { object: launch, from: '2019-02-03T17:04:05Z' });
  // Raj co-founded Acme, undated.
  await claim(P, raj, 'founded', { object: acme });
  // Acme: founding year, a funding milestone, an acquisition, a launch posted by Maya (no edge).
  await claim(P, acme, 'founded_year', { value: 2018 });
  await claim(P, acme, 'milestone', { value: { title: 'Raised a $4M seed round', kind: 'funding', url: 'https://example.com/seed' }, from: '2019-09-10' });
  await claim(P, bigco, 'acquired', { object: acme, from: '2023-01-15' });

  // A merged-away duplicate of Maya carries one dated fact; it must show on Maya's timeline.
  await claim(P, old, 'attended', { object: launch, from: '2016-01-01' }).catch(() => {});
  await t.owner`update entity set merged_into = ${maya} where id = ${old}`;

  // Private, dated facts (alice only): a job and a milestone on Maya, and an edge from a private entity.
  await claim(PRIV, maya, 'worked_at', { object: bigco, value: { title: 'Secret Role' }, from: '2022-02-02', to: '2022-12-12', ref: 'priv:job' });
  await claim(PRIV, maya, 'milestone', { value: { title: 'Secret milestone', kind: 'personal' }, from: '2020-04-04', ref: 'priv:ms' });
  await claim(PRIV, secretCo, 'acquired', { object: acme, from: '2024-04-04', ref: 'priv:acq' });
});

type Ev = { at: string; kind: string; title: string; precision: string; headline: boolean; key: string; other: { id: string } | null; fact: any };
const timeline = async (eid: string, headers?: Record<string, string>, qs = '') => {
  const r = await t.get(`/v0/entities/${encodeURIComponent(eid)}/timeline${qs}`, headers);
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body as { id: string; events: Ev[]; next: string | null };
};
const summary = (evs: Ev[]) => evs.filter((e) => e.kind !== 'first_seen').map((e) => `${e.at.slice(0, 10)} ${e.kind} ${e.title}`);

describe('GET /v0/entities/:id/timeline', () => {
  it('orders events newest first; ranges give joined/left, point-in-time predicates one event', async () => {
    const body = await timeline(maya);
    expect(body.id).toBe(maya);
    expect(summary(body.events)).toEqual([
      '2021-05-01 joined Joined BigCo Tl as Advisor',
      '2019-02-03 posted Posted Acme v1',
      '2018-01-01 founded Founded Acme Tl',
      '2017-06-30 left Left Widgets Tl',
      '2016-01-01 attended Attended Acme v1',
      '2014-03-01 joined Joined Widgets Tl as Engineer',
    ]);
    const ats = body.events.map((e) => e.at);
    expect([...ats].sort().reverse()).toEqual(ats);
    // first seen in Rebox is the newest event (fixtures were just inserted).
    expect(body.events[0]!.kind).toBe('first_seen');
    // Point-in-time: one event per post, never a "left" for it.
    expect(body.events.filter((e) => e.fact?.predicate === 'posted')).toHaveLength(1);
    const posted = body.events.find((e) => e.kind === 'posted')!;
    expect(posted.precision).toBe('time');
    expect(posted.other!.id).toBe(launch);
    expect(posted.fact.source).toBe('s10tl');
    // Founding of Acme comes from Acme's founded_year: year precision, headline.
    const founded = body.events.find((e) => e.kind === 'founded')!;
    expect(founded).toMatchObject({ precision: 'year', headline: true });
    expect(new Set(body.events.map((e) => e.key)).size).toBe(body.events.length);
  });

  it('company timeline: founders, milestones, acquisitions, founders\' launches', async () => {
    const body = await timeline(acme);
    expect(summary(body.events)).toEqual([
      '2023-01-15 acquired Acquired by BigCo Tl',
      '2019-09-10 milestone Raised a $4M seed round',
      '2019-02-03 posted Maya Tl posted Acme v1',
      '2018-01-01 founded Founded by Maya Tl and Raj Tl',
    ]);
    const byKind = Object.fromEntries(body.events.map((e) => [e.kind, e]));
    expect(byKind.milestone!.headline).toBe(true);
    expect(byKind.acquired!.headline).toBe(true);
    expect(byKind.founded!.precision).toBe('year');
    expect(byKind.milestone!.precision).toBe('day');
  });

  it('paginates with a cursor without gaps or repeats', async () => {
    const all = (await timeline(maya)).events.map((e) => e.key);
    const seen: string[] = [];
    let cursor: string | null = null;
    do {
      const page: { events: Ev[]; next: string | null } = await timeline(maya, undefined, `?limit=2${cursor ? `&cursor=${cursor}` : ''}`);
      expect(page.events.length).toBeLessThanOrEqual(2);
      seen.push(...page.events.map((e) => e.key));
      cursor = page.next;
    } while (cursor);
    expect(seen).toEqual(all);
    expect((await t.get(`/v0/entities/${maya}/timeline?cursor=nope`)).status).toBe(400);
    expect((await t.get(`/v0/entities/${maya}/timeline?limit=0`)).status).toBe(400);
  });

  it('never leaks private dated facts (RLS through the fold)', async () => {
    for (const h of [undefined, bob]) {
      const m = JSON.stringify(await timeline(maya, h));
      expect(m).not.toContain('Secret');
      expect(m).not.toContain(PRIV);
      const a = JSON.stringify(await timeline(acme, h));
      expect(a).not.toContain(secretCo);
      expect(a).not.toContain('Secret Stealth');
    }
    const m = summary((await timeline(maya, alice)).events);
    expect(m).toContain('2022-12-12 left Left BigCo Tl');
    expect(m).toContain('2020-04-04 milestone Secret milestone');
    expect(summary((await timeline(acme, alice)).events)).toContain('2024-04-04 acquired Acquired by Secret Stealth Co');
  });

  it('merged-away ids resolve to the target; unknown and private ids are 404', async () => {
    const body = await timeline(old);
    expect(body.id).toBe(maya);
    expect((await t.get('/v0/entities/rbx:person:s10tl-nobody/timeline')).status).toBe(404);
    expect((await t.get(`/v0/entities/${secretCo}/timeline`)).status).toBe(404);
    expect((await t.get(`/v0/entities/${secretCo}/timeline`, alice)).status).toBe(200);
  });
});

describe('timeline and reason formatting helpers', () => {
  it('precision', () => {
    expect(precisionOf('2018-01-01T00:00:00.000Z', 'founded_year')).toBe('year');
    expect(precisionOf('2018-01-01T00:00:00.000Z', 'worked_at')).toBe('year');
    expect(precisionOf('2018-06-28T00:00:00.000Z', 'launched')).toBe('day');
    expect(precisionOf('2018-06-28T17:23:50.000Z', 'launched')).toBe('time');
  });

  it('titles read from either side', () => {
    const l = (p: string) => p;
    expect(describeEdge('worked_at', 'in', 'start', 'Ann', { title: 'CTO' }, l)).toEqual({ kind: 'joined', title: 'Ann joined as CTO' });
    expect(describeEdge('launched', 'in', 'point', 'Acme', null, l)).toEqual({ kind: 'launched', title: 'Launched by Acme' });
    expect(joinNames(['A', 'B', 'C', 'D'])).toBe('A, B and 2 others');
  });

  it('reason spans never end in a dash', () => {
    expect(formatSpan('2019-01-01', '2021-01-01')).toBe(' 2019–2021');
    expect(formatSpan('2024-03-01', null)).toBe(' since 2024');
    expect(formatSpan(null, '2021-01-01')).toBe(' until 2021');
    expect(formatSpan('2018-06-28', null, true)).toBe(' 2018');
    expect(formatSpan(null, null)).toBe('');
  });
});
