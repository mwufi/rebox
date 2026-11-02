import { beforeAll, describe, expect, it } from 'vitest';
import { nameClaim, ref, setupApp } from './helpers.js';

const t = setupApp({ env: 'dev' });
const alice = { 'x-rebox-viewer': 'alice' };

// Fixed, query-specific ids (prefix qx-) so result lists are exact.
const stripe = 'rbx:company:qx-stripe';
const acme = 'rbx:company:qx-acme';
const maya = 'rbx:person:qx-maya-chen';
const li = 'rbx:person:qx-li-chen';
const sam = 'rbx:person:qx-sam-early';
const ana = 'rbx:person:qx-ana-later';
const old = 'rbx:person:qx-old-chen';

async function q(body: unknown, headers?: Record<string, string>) {
  return t.post('/v0/query', body, headers);
}
const ids = (r: { body: { results: { id: string }[] } }) => r.body.results.map((x) => x.id);

beforeAll(async () => {
  await t.owner`insert into layer (id, scope, owner, source) values ('user/alice/gmail', 'user', 'alice', 'gmail') on conflict do nothing`;
  const w = (subject: string, object: string, from: string | null, to: string | null, title: string, key: string) => ({
    subject: ref(subject), predicate: 'worked_at', object: ref(object), value: { title },
    valid_from: from, valid_to: to, source_ref: `qx:${key}`,
  });
  const r = await t.write('public/qtest', [
    nameClaim(stripe, 'QX Stripe'),
    nameClaim(acme, 'QX Acme'),
    nameClaim(maya, 'Maya Qxchen'),
    nameClaim(li, 'Li Qxchen'),
    nameClaim(sam, 'Sam Early'),
    nameClaim(ana, 'Ana Later'),
    nameClaim(old, 'Oldname Qxchen'),
    { subject: ref(stripe), predicate: 'yc_batch', value: 'QX09', source_ref: 'qx:s:yc' },
    // Values unique to this file: other test files share the database.
    { subject: ref(acme), predicate: 'yc_batch', value: 'QX24', source_ref: 'qx:a:yc' },
    { subject: ref(stripe), predicate: 'headcount', value: 8000, source_ref: 'qx:s:hc' },
    { subject: ref(acme), predicate: 'headcount', value: 12, source_ref: 'qx:a:hc' },
    { subject: ref(stripe), predicate: 'domain', value: ['qx-stripe.example', 'qx-stripe.dev'], source_ref: 'qx:s:dom' },
    w(maya, stripe, '2019-03-01', '2021-02-01', 'CTO', 'maya'),
    w(li, stripe, '2022-01-01', null, 'Engineer', 'li'),
    w(sam, stripe, '2010-01-01', '2012-12-31', 'Engineer', 'sam'),
    w(ana, stripe, '2021-12-31', '2023-01-01', 'PM', 'ana'),
    w(old, acme, '2015-01-01', '2016-01-01', 'Intern', 'old'),
    { subject: ref(maya), predicate: 'founded', object: ref(acme), source_ref: 'qx:maya:founded' },
  ]);
  expect(r.status).toBe(200);
});

describe('POST /v0/query', () => {
  it('worked_at with during overlap', async () => {
    const r = await q({ find: 'person', where: [{ edge: { predicate: 'worked_at', dir: 'out', to: stripe, during: { from: '2018-01-01', to: '2021-12-31' } } }] });
    expect(r.status).toBe(200);
    // maya overlaps; ana starts on the last day (inclusive); li starts after; sam ended before.
    expect(ids(r)).toEqual([ana, maya]);
    const m = r.body.results.find((x: any) => x.id === maya);
    expect(m).toEqual({ id: maya, kind: 'person', name: 'Maya Qxchen', reasons: ['worked_at QX Stripe 2019–2021'] });
  });

  it('open-ended validity and edge without `to`', async () => {
    const r = await q({ find: 'person', where: [{ edge: { predicate: 'worked_at', to: stripe, during: { from: '2024-06-01' } } }] });
    expect(ids(r)).toEqual([li]);
    expect(r.body.results[0].reasons).toEqual(['worked_at QX Stripe 2022–']);
    const any = await q({ find: 'person', where: [{ name: 'qxchen' }, { edge: { predicate: 'founded' } }] });
    expect(ids(any)).toEqual([maya]);
  });

  it('dir in matches edges pointing at the result', async () => {
    const r = await q({ find: 'company', where: [{ edge: { predicate: 'founded', dir: 'in', to: maya } }] });
    expect(ids(r)).toEqual([acme]);
    expect(r.body.results[0].reasons).toEqual(['Maya Qxchen founded']);
  });

  it('attr eq / gte / lte / contains', async () => {
    expect(ids(await q({ find: 'company', where: [{ attr: { predicate: 'yc_batch', eq: 'QX24' } }] }))).toEqual([acme]);
    const big = await q({ find: 'company', where: [{ name: 'QX' }, { attr: { predicate: 'headcount', gte: 100 } }] });
    expect(ids(big)).toEqual([stripe]);
    expect(big.body.results[0].reasons).toEqual(['name QX Stripe', 'headcount 8000']);
    expect(ids(await q({ find: 'company', where: [{ name: 'QX' }, { attr: { predicate: 'headcount', lte: 12 } }] }))).toEqual([acme]);
    expect(ids(await q({ find: 'company', where: [{ name: 'QX' }, { attr: { predicate: 'yc_batch', gte: 'QX10', lte: 'QX30' } }] }))).toEqual([acme]);
    expect(ids(await q({ find: 'company', where: [{ name: 'QX' }, { attr: { predicate: 'yc_batch', contains: 'x0' } }] }))).toEqual([stripe]);
    expect(ids(await q({ find: 'company', where: [{ attr: { predicate: 'headcount', eq: 8000 } }, { name: 'qx' }] }))).toEqual([stripe]);
  });

  it('name filter', async () => {
    const r = await q({ find: 'person', where: [{ name: 'qxchen' }] });
    expect(ids(r)).toEqual([li, maya, old]);
    expect(r.body.results.find((x: any) => x.id === li).reasons).toEqual(['name Li Qxchen']);
  });

  it('combined AND with reasons for every filter', async () => {
    const r = await q({
      find: 'person',
      where: [
        { name: 'qxchen' },
        { edge: { predicate: 'worked_at', to: stripe, during: { from: '2018-01-01', to: '2021-12-31' } } },
        { edge: { predicate: 'founded', dir: 'out' } },
      ],
    });
    expect(r.body.results).toEqual([
      { id: maya, kind: 'person', name: 'Maya Qxchen', reasons: ['name Maya Qxchen', 'worked_at QX Stripe 2019–2021', 'founded QX Acme'] },
    ]);
    expect(r.body.next).toBeNull();
  });

  it('matches only effective (folded) facts, per viewer', async () => {
    // alice renames "Oldname Chen" privately and retracts maya's founded edge.
    await t.owner`insert into claim (layer_id, subject, predicate, value, source_ref)
                  values ('user/alice/gmail', ${old}, 'name', '"Qxrenamed Person"', 'qx:alice:name')`;
    await t.owner`insert into claim (layer_id, subject, predicate, object_id, retract, source_ref)
                  values ('user/alice/gmail', ${maya}, 'founded', ${acme}, true, 'qx:alice:founded')`;
    expect(ids(await q({ find: 'person', where: [{ name: 'qxchen' }] }))).toEqual([li, maya, old]);
    expect(ids(await q({ find: 'person', where: [{ name: 'qxchen' }] }, alice))).toEqual([li, maya]);
    expect(ids(await q({ find: 'person', where: [{ name: 'qxrenamed' }] }, alice))).toEqual([old]);
    expect(ids(await q({ find: 'person', where: [{ name: 'qxrenamed' }] }))).toEqual([]);
    const founders = { find: 'person', where: [{ name: 'qxchen' }, { edge: { predicate: 'founded' } }] };
    expect(ids(await q(founders))).toEqual([maya]);
    expect(ids(await q(founders, alice))).toEqual([]);
  });

  it('paginates with an opaque cursor', async () => {
    const first = await q({ find: 'person', where: [{ edge: { predicate: 'worked_at', to: stripe } }], limit: 2 });
    expect(first.body.results).toHaveLength(2);
    expect(first.body.next).toEqual(expect.any(String));
    const second = await q({ find: 'person', where: [{ edge: { predicate: 'worked_at', to: stripe } }], limit: 2, cursor: first.body.next });
    expect(second.body.next).toBeNull();
    expect([...ids(first), ...ids(second)]).toEqual([ana, li, maya, sam]);
  });

  it('rejects unknown predicates, non-filterable attributes and bad shapes with 400', async () => {
    const bad = [
      [{ find: 'person', where: [{ attr: { predicate: 'favorite_color', eq: 'red' } }] }, /unknown predicate/],
      [{ find: 'company', where: [{ attr: { predicate: 'description', contains: 'x' } }] }, /not filterable/],
      [{ find: 'person', where: [{ edge: { predicate: 'knows' } }] }, /not filterable/],
      [{ find: 'company', where: [{ attr: { predicate: 'worked_at', eq: 'x' } }] }, /is an edge/],
      [{ find: 'company', where: [{ edge: { predicate: 'yc_batch' } }] }, /is an attribute/],
      [{ find: 'company', where: [{ attr: { predicate: 'headcount', gte: 'ten' } }] }, /must be a number/],
      [{ find: 'person', where: [{ attr: { predicate: 'yc_batch', eq: 'W24' } }] }, /does not apply/],
      [{ find: 'robot', where: [] }, /unknown kind/],
      [{ find: 'person', where: [{ edge: { predicate: 'worked_at', during: { from: '2021-02-30' } } }] }, /ISO date/],
      [{ find: 'person', where: [{ bogus: 1 }] }, /filter must be one of/],
      [{ find: 'person', where: [], cursor: 'not-a-cursor' }, /invalid cursor/],
    ] as const;
    for (const [body, message] of bad) {
      const r = await q(body);
      expect(r.status, JSON.stringify(body)).toBe(400);
      expect(JSON.stringify(r.body.details)).toMatch(message);
    }
  });
});
