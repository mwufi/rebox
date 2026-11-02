/**
 * S13: the paged fold paths (foldEdgePage, foldEdgeGroups, and the entity view built on them) must
 * return exactly the rows the full fold (foldFacts) returns for the same keys: same winners, same
 * support, same order. Fixtures cover every rule the fold has: layer scope, worker trust, newest
 * write, root-over-merged, highest id, retracted winners and losers, merges on both sides, an
 * invisible other side, and duplicate (layer, source_ref) rows.
 */
import { beforeAll, describe, expect, it } from 'vitest';
import type { Tx } from '../src/db.js';
import { foldEdgeGroups, foldEdgePage, foldFacts, type Dir, type FoldedRow } from '../src/fold.js';
import { toFacts, VIEW_EDGES_PER_PREDICATE } from '../src/views.js';
import { setupApp, uid } from './helpers.js';

const t = setupApp({ env: 'dev' });

const PUB = 'public/s13a';
const PUB2 = 'public/s13b';
const TEAM = 'team/s13team/crm';
const ALICE = 'user/s13alice/gmail';
const LOW = 's13-connector-000001'; // trust 0.2
const HIGH = 's13-connector-000002'; // trust 0.9

const ids = {
  hub: uid('company', 'hub'),
  hubOld: uid('company', 'hubold'), // merged into hub: its claims count on hub (direct = false)
  person: uid('person', 'p'),
  personOld: uid('person', 'pold'), // merged into person: edges to it fold onto person
  priv: uid('person', 'priv'), // home layer is alice's: invisible to everyone else
  others: Array.from({ length: 9 }, (_, i) => uid('person', `o${i}`)),
  cos: Array.from({ length: 4 }, (_, i) => uid('company', `c${i}`)),
};

let n = 0;
/** Inserts a claim as the owner (bypasses RLS and writer validation) with full control. */
async function claim(
  layer: string,
  subject: string,
  predicate: string,
  object: string | null,
  o: { value?: unknown; retract?: boolean; worker?: string | null; at?: string; ref?: string } = {},
) {
  n += 1;
  await t.owner`
    insert into claim (layer_id, subject, predicate, object_id, value, retract, source_ref, worker_id, updated_at)
    values (${layer}, ${subject}, ${predicate}, ${object}, ${o.value === undefined ? null : t.owner.json(o.value as never)},
            ${o.retract ?? false}, ${o.ref ?? `s13:${n}`}, ${o.worker ?? null}, ${o.at ?? '2026-01-01T00:00:00Z'})`;
}

beforeAll(async () => {
  await t.owner`
    insert into layer (id, scope, owner, source) values
      (${PUB}, 'public', null, 's13a'), (${PUB2}, 'public', null, 's13b'),
      (${TEAM}, 'team', 's13team', 'crm'), (${ALICE}, 'user', 's13alice', 'gmail')
    on conflict do nothing`;
  await t.owner`insert into team_member values ('s13team', 's13carol') on conflict do nothing`;
  await t.owner`
    insert into worker (id, kind, source, trust, layers) values
      (${LOW}, 'connector', 's13', 0.2, ${[PUB, PUB2]}), (${HIGH}, 'connector', 's13', 0.9, ${[PUB, PUB2]})
    on conflict do nothing`;
  const all = [ids.hub, ids.hubOld, ids.person, ids.personOld, ...ids.others, ...ids.cos];
  for (const id of all) {
    await t.owner`insert into entity (id, kind, home_layer) values (${id}, ${id.split(':')[1]!}, ${PUB})`;
    await claim(PUB, id, 'name', null, { value: `Name ${id.slice(-8)}` });
  }
  await t.owner`insert into entity (id, kind, home_layer) values (${ids.priv}, 'person', ${ALICE})`;
  await claim(ALICE, ids.priv, 'name', null, { value: 'Private Person' });

  const [o0, o1, o2, o3, o4, o5, o6, o7, o8] = ids.others as [string, string, string, string, string, string, string, string, string];
  const { hub, hubOld, person, personOld, priv } = ids;
  // o0: scope — public says one thing, the team layer and alice's layer override for their viewers.
  await claim(PUB, o0, 'worked_at', hub, { value: { title: 'pub' }, worker: HIGH, at: '2026-03-01T00:00:00Z' });
  await claim(TEAM, o0, 'worked_at', hub, { value: { title: 'team' }, at: '2025-01-01T00:00:00Z' });
  await claim(ALICE, o0, 'worked_at', hub, { value: { title: 'alice' }, at: '2024-01-01T00:00:00Z' });
  // o1: trust beats a newer write within a scope; support counts both sources.
  await claim(PUB, o1, 'worked_at', hub, { value: { title: 'high' }, worker: HIGH, at: '2026-01-01T00:00:00Z' });
  await claim(PUB2, o1, 'worked_at', hub, { value: { title: 'low' }, worker: LOW, at: '2026-06-01T00:00:00Z' });
  // o2: same trust (unattributed), newest write wins.
  await claim(PUB, o2, 'worked_at', hub, { value: { title: 'old' }, at: '2026-01-01T00:00:00Z' });
  await claim(PUB2, o2, 'worked_at', hub, { value: { title: 'new' }, at: '2026-02-01T00:00:00Z' });
  // o3: full tie except root-vs-merged: the claim on hub itself beats the one inherited from hubOld.
  await claim(PUB, o3, 'worked_at', hubOld, { value: { title: 'via old' }, at: '2026-01-01T00:00:00Z', ref: 's13:o3' });
  await claim(PUB2, o3, 'worked_at', hub, { value: { title: 'direct' }, at: '2026-01-01T00:00:00Z' });
  // o4: complete tie: highest id wins.
  await claim(PUB, o4, 'worked_at', hub, { value: { title: 'first' } });
  await claim(PUB2, o4, 'worked_at', hub, { value: { title: 'second' } });
  // o5: alice retracts a public edge (the winner for alice is a retraction).
  await claim(PUB, o5, 'worked_at', hub, { value: { title: 'pub' } });
  await claim(ALICE, o5, 'worked_at', hub, { retract: true });
  // o6: an old public retraction loses to a newer assertion: the key stays alive.
  await claim(PUB, o6, 'worked_at', hub, { retract: true, at: '2025-01-01T00:00:00Z' });
  await claim(PUB2, o6, 'worked_at', hub, { value: { title: 'back' }, at: '2026-01-01T00:00:00Z' });
  // o7: duplicate (layer, source_ref) on root and merged member: support counts it once.
  await claim(PUB, o7, 'founded', hub, { ref: 's13:dup' });
  await claim(PUB, o7, 'founded', hubOld, { ref: 's13:dup' });
  await claim(PUB2, o7, 'founded', hub, {});
  // o8 and the merged person: other-side merges fold onto the canonical id.
  await claim(PUB, o8, 'founded', hub, {});
  await claim(PUB, personOld, 'founded', hub, { ref: 's13:pold' });
  await claim(PUB2, person, 'founded', hub, {});
  await claim(PUB, person, 'invested_in', hubOld, {});
  // A public claim pointing at alice's private entity: only alice may see the edge.
  await claim(PUB, priv, 'worked_at', hub, {});
  // Outgoing edges of the person, including a retraction for alice and a private target.
  for (const [i, c] of ids.cos.entries()) await claim(PUB, person, 'worked_at', c, { value: { title: `t${i}` }, worker: i % 2 ? HIGH : LOW });
  await claim(ALICE, person, 'worked_at', ids.cos[0]!, { retract: true });
  await claim(ALICE, person, 'knows', priv, { value: { strength: 1 } });
  await claim(PUB, personOld, 'knows', o0, {});
  await claim(PUB, person, 'knows', o0, {});
  await claim(PUB, person, 'knows', o1, {});
  // Incoming edges of the person, one through its merged-away id.
  await claim(PUB, o1, 'knows', personOld, {});
  await claim(PUB2, o2, 'knows', person, {});
  await claim(PUB, o2, 'knows', personOld, { retract: true, at: '2025-01-01T00:00:00Z' });

  await t.owner`update entity set merged_into = ${hub} where id = ${hubOld}`;
  await t.owner`update entity set merged_into = ${person} where id = ${personOld}`;
});

type Viewer = { name: string; visible: () => Promise<string[]> };
const publicLayers = async () => (await t.owner<{ id: string }[]>`select id from layer where scope = 'public'`).map((r) => r.id);
const VIEWERS: Viewer[] = [
  { name: 'anonymous', visible: publicLayers },
  { name: 'team member', visible: async () => [...(await publicLayers()), TEAM] },
  { name: 'alice', visible: async () => [...(await publicLayers()), ALICE] },
  // Every layer visible: exercises the shortcut that skips per-key entity probes.
  { name: 'all layers', visible: async () => (await t.owner<{ id: string }[]>`select id from layer`).map((r) => r.id) },
];

async function asViewer<T>(v: Viewer, fn: (tx: Tx) => Promise<T>): Promise<T> {
  return t.db.withView({ visible: await v.visible(), writable: [] }, fn);
}

/** Pages through foldEdgePage with a small limit, following the cursor. */
async function allPages(tx: Tx, root: string, dir: Dir, predicates: string[] | undefined, limit: number) {
  const rows: FoldedRow[] = [];
  let after: { predicate: string; other: string } | null = null;
  for (let i = 0; i < 50; i++) {
    const page = await foldEdgePage(tx, { root, dir, predicates, after, limit });
    rows.push(...page);
    if (page.length < limit) return rows;
    const last = page[page.length - 1]!;
    after = { predicate: last.predicate, other: last.other! };
  }
  throw new Error('too many pages');
}

const byPredicate = (rows: FoldedRow[]) => {
  const m = new Map<string, FoldedRow[]>();
  for (const r of rows) m.set(r.predicate, [...(m.get(r.predicate) ?? []), r]);
  return m;
};

describe('paged fold = full fold for the rows returned', () => {
  for (const v of VIEWERS) {
    for (const [label, root, dir] of [
      ['hub incoming', () => ids.hub, 'in'],
      ['person outgoing', () => ids.person, 'out'],
      ['person incoming (anchor merged)', () => ids.person, 'in'],
      ['incoming from a merged subject', () => ids.others[0]!, 'in'],
    ] as const) {
      it(`${label} as ${v.name}`, async () => {
        await asViewer(v, async (tx) => {
          const ref = await foldFacts(tx, { roots: [root()], dir, edgesOnly: true });
          expect(ref.length).toBeGreaterThan(0);
          // Every page size, all predicates and one predicate at a time.
          for (const limit of [1, 2, 3, 100]) expect(await allPages(tx, root(), dir, undefined, limit)).toEqual(ref);
          for (const [p, rows] of byPredicate(ref)) expect(await allPages(tx, root(), dir, [p], 2)).toEqual(rows);
          // Groups: the first N per predicate, and exact totals.
          for (const per of [1, 2, 50]) {
            const g = await foldEdgeGroups(tx, { root: root(), dir, perPredicate: per });
            const want = [...byPredicate(ref)].flatMap(([, rows]) => rows.slice(0, per));
            expect(g.rows).toEqual(want);
            expect(g.counts).toEqual(Object.fromEntries([...byPredicate(ref)].map(([p, rows]) => [p, rows.length])));
          }
        });
      });
    }
  }

  it('the fixtures exercise the rules they are meant to', async () => {
    const anon = await asViewer(VIEWERS[0]!, (tx) => foldFacts(tx, { roots: [ids.hub], dir: 'in' }));
    const team = await asViewer(VIEWERS[1]!, (tx) => foldFacts(tx, { roots: [ids.hub], dir: 'in' }));
    const alice = await asViewer(VIEWERS[2]!, (tx) => foldFacts(tx, { roots: [ids.hub], dir: 'in' }));
    const [o0, o1, o2, o3, o4, o5, o6, o7] = ids.others;
    const get = (rows: FoldedRow[], p: string, other: string) => rows.find((r) => r.predicate === p && r.other === other);
    expect((get(anon, 'worked_at', o0!)!.value as any).title).toBe('pub');
    expect((get(team, 'worked_at', o0!)!.value as any).title).toBe('team');
    expect((get(alice, 'worked_at', o0!)!.value as any).title).toBe('alice');
    expect((get(anon, 'worked_at', o1!)!.value as any).title).toBe('high');
    expect(get(anon, 'worked_at', o1!)!.support).toBe(2);
    expect((get(anon, 'worked_at', o2!)!.value as any).title).toBe('new');
    expect((get(anon, 'worked_at', o3!)!.value as any).title).toBe('direct');
    expect((get(anon, 'worked_at', o4!)!.value as any).title).toBe('second');
    expect(get(anon, 'worked_at', o5!)).toBeTruthy();
    expect(get(alice, 'worked_at', o5!)).toBeUndefined();
    expect((get(anon, 'worked_at', o6!)!.value as any).title).toBe('back');
    expect(get(anon, 'worked_at', o6!)!.support).toBe(1);
    expect(get(anon, 'founded', o7!)!.support).toBe(2);
    expect(get(anon, 'founded', ids.person)!.support).toBe(2);
    expect(get(anon, 'worked_at', ids.priv)).toBeUndefined();
    expect(get(alice, 'worked_at', ids.priv)).toBeTruthy();
  });
});

describe('entity view (S13)', () => {
  it('a small entity view equals the full fold, plus counts', async () => {
    const cases: [Record<string, string>, string[]][] = [
      [{}, []],
      [{ 'x-rebox-viewer': 's13alice' }, [ALICE]],
      [{ 'x-rebox-viewer': 's13carol' }, [TEAM]],
    ];
    for (const [viewer, extra] of cases) {
      const v = (await t.get(`/v0/entities/${ids.person}`, viewer)).body;
      const visible = [...(await publicLayers()), ...extra];
      const old = await t.db.withView({ visible, writable: [] }, async (tx) => {
        const out = await foldFacts(tx, { roots: [ids.person], dir: 'out' });
        const inc = await foldFacts(tx, { roots: [ids.person], dir: 'in', page: { after: null, limit: 500 } });
        return { facts: await toFacts(tx, out, 'out'), incoming: await toFacts(tx, inc, 'in') };
      });
      expect(v.facts).toEqual(old.facts);
      expect(v.incoming).toEqual(old.incoming);
      const count = (facts: any[]) =>
        facts.filter((f) => f.object).reduce((m: Record<string, number>, f) => ({ ...m, [f.predicate]: (m[f.predicate] ?? 0) + 1 }), {});
      expect(v.facts_counts).toEqual(count(old.facts.filter((f) => f.object)));
      expect(v.incoming_counts).toEqual(count(old.incoming));
    }
  });

  it('caps each edge predicate at VIEW_EDGES_PER_PREDICATE and the edges endpoint continues', async () => {
    const big = uid('company', 'bighub');
    const N = VIEW_EDGES_PER_PREDICATE + 5;
    await t.owner`insert into entity (id, kind, home_layer) values (${big}, 'company', ${PUB})`;
    await claim(PUB, big, 'name', null, { value: 'Big Hub' });
    const people: string[] = [];
    for (let i = 0; i < N; i++) {
      const p = uid('person', `bh${i}`);
      people.push(p);
      await t.owner`insert into entity (id, kind, home_layer) values (${p}, 'person', ${PUB})`;
      await claim(PUB, p, 'name', null, { value: `BH ${i}` });
      await claim(PUB, p, 'worked_at', big, {});
      await claim(PUB, big, 'invested_in', ids.cos[i % ids.cos.length]!, { ref: `s13:inv:${i}` }); // 4 distinct keys
    }
    await claim(PUB, people[0]!, 'founded', big, {});
    const v = (await t.get(`/v0/entities/${big}`)).body;
    expect(v.incoming_counts).toEqual({ worked_at: N, founded: 1 });
    expect(v.facts_counts).toEqual({ invested_in: 4 });
    const worked = v.incoming.filter((f: any) => f.predicate === 'worked_at');
    expect(worked).toHaveLength(VIEW_EDGES_PER_PREDICATE);
    expect(worked.map((f: any) => f.subject.id)).toEqual([...people].sort().slice(0, VIEW_EDGES_PER_PREDICATE));
    // The edges endpoint, same order: its first page is the view's rows, then the rest.
    const p1 = (await t.get(`/v0/entities/${big}/edges?dir=in&predicate=worked_at&limit=${VIEW_EDGES_PER_PREDICATE}`)).body;
    expect(p1.edges).toEqual(worked);
    const p2 = (await t.get(`/v0/entities/${big}/edges?dir=in&predicate=worked_at&limit=50&cursor=${p1.next}`)).body;
    expect(p2.edges.map((f: any) => f.subject.id)).toEqual([...people].sort().slice(VIEW_EDGES_PER_PREDICATE));
    expect(p2.next).toBeNull();
  });

  it('a merged id redirects with the counts on the target view', async () => {
    const r = (await t.get(`/v0/entities/${ids.hubOld}`)).body;
    expect(r.redirect).toBe(ids.hub);
    expect(r.entity.incoming_counts.worked_at).toBe(r.entity.incoming.filter((f: any) => f.predicate === 'worked_at').length);
  });
});
