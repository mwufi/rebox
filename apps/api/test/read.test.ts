import { beforeAll, describe, expect, it } from 'vitest';
import { nameClaim, ref, setupApp, uid } from './helpers.js';

const t = setupApp({ env: 'dev' });
const alice = { 'x-rebox-viewer': 'alice' };
const bob = { 'x-rebox-viewer': 'bob' };
const carol = { 'x-rebox-viewer': 'carol' };

/** Inserts private-layer claims directly as the owner (bypasses RLS), like a private connector would. */
async function ownerClaim(layer: string, subject: string, predicate: string, value: unknown, opts: { object?: string; retract?: boolean; ref?: string } = {}) {
  await t.owner`
    insert into claim (layer_id, subject, predicate, object_id, value, retract, source_ref)
    values (${layer}, ${subject}, ${predicate}, ${opts.object ?? null}, ${value === null ? null : t.owner.json(value as never)},
            ${opts.retract ?? false}, ${opts.ref ?? `${layer}:${subject}:${predicate}`})`;
}

const names = (facts: { predicate: string; value: unknown }[], p: string) =>
  facts.filter((f) => f.predicate === p).map((f) => f.value);

beforeAll(async () => {
  await t.owner`
    insert into layer (id, scope, owner, source) values
      ('user/alice/gmail', 'user', 'alice', 'gmail'),
      ('user/bob/gmail', 'user', 'bob', 'gmail'),
      ('team/acme/crm', 'team', 'acme', 'crm')
    on conflict do nothing`;
  await t.owner`insert into team_member values ('acme', 'carol') on conflict do nothing`;
});

describe('RLS through the API (app role)', () => {
  it('a user/alice claim is visible to alice only', async () => {
    const p = uid('person', 'rls');
    expect((await t.write('public/test', [nameClaim(p, 'Rls Person')])).status).toBe(200);
    await ownerClaim('user/alice/gmail', p, 'headline', 'Secret headline');

    const anon = await t.get(`/v0/entities/${p}`);
    expect(anon.status).toBe(200);
    expect(names(anon.body.facts, 'headline')).toEqual([]);
    expect(JSON.stringify(anon.body)).not.toContain('Secret');
    const b = await t.get(`/v0/entities/${p}`, bob);
    expect(names(b.body.facts, 'headline')).toEqual([]);
    const a = await t.get(`/v0/entities/${p}`, alice);
    expect(names(a.body.facts, 'headline')).toEqual(['Secret headline']);
    expect(a.body.facts.find((f: any) => f.predicate === 'headline').layer).toBe('user/alice/gmail');
  });

  it('team layers are visible to team members', async () => {
    const p = uid('person', 'team');
    await t.write('public/test', [nameClaim(p, 'Team Person')]);
    await ownerClaim('team/acme/crm', p, 'headline', 'Team note');
    expect(names((await t.get(`/v0/entities/${p}`, carol)).body.facts, 'headline')).toEqual(['Team note']);
    expect(names((await t.get(`/v0/entities/${p}`, bob)).body.facts, 'headline')).toEqual([]);
  });

  it('a private entity is a 404 for others, and its edges never leak', async () => {
    const pub = uid('person', 'pub');
    await t.write('public/test', [nameClaim(pub, 'Has Private Friend')]);
    const priv = uid('person', 'priv');
    await t.owner`insert into entity (id, kind, home_layer) values (${priv}, 'person', 'user/alice/gmail')`;
    await ownerClaim('user/alice/gmail', priv, 'name', 'Hidden Friend');
    await ownerClaim('user/alice/gmail', pub, 'knows', { strength: 1 }, { object: priv });

    expect((await t.get(`/v0/entities/${priv}`)).status).toBe(404);
    expect((await t.get(`/v0/entities/${priv}`, bob)).status).toBe(404);
    expect((await t.get(`/v0/entities/${priv}`, alice)).body.name).toBe('Hidden Friend');
    expect(JSON.stringify((await t.get(`/v0/entities/${pub}`, bob)).body)).not.toContain(priv);
    const a = await t.get(`/v0/entities/${pub}`, alice);
    expect(a.body.facts.find((f: any) => f.predicate === 'knows').object).toEqual({ id: priv, kind: 'person', name: 'Hidden Friend' });

    // Private names are searchable by their owner only.
    const hitIds = async (h?: Record<string, string>) =>
      (await t.get('/v0/search?q=hidden%20friend', h)).body.map((x: any) => x.id);
    expect(await hitIds()).not.toContain(priv);
    expect(await hitIds(bob)).not.toContain(priv);
    expect(await hitIds(alice)).toContain(priv);
  });

  it('ignores X-Rebox-Viewer outside dev', async () => {
    const prod = setupApp({ env: 'prod' });
    const p = uid('person', 'prod');
    await prod.write('public/test', [nameClaim(p, 'Prod Person')]);
    await ownerClaim('user/alice/gmail', p, 'headline', 'Prod secret');
    expect(names((await prod.get(`/v0/entities/${p}`, alice)).body.facts, 'headline')).toEqual([]);
  });
});

describe('fold', () => {
  it('a user name overrides the public name for that viewer only', async () => {
    const p = uid('person', 'fold');
    await t.write('public/test', [nameClaim(p, 'Public Name')]);
    await ownerClaim('user/alice/gmail', p, 'name', 'Alice Calls Them Bobby');
    expect((await t.get(`/v0/entities/${p}`)).body.name).toBe('Public Name');
    expect((await t.get(`/v0/entities/${p}`, bob)).body.name).toBe('Public Name');
    const a = (await t.get(`/v0/entities/${p}`, alice)).body;
    expect(a.name).toBe('Alice Calls Them Bobby');
    expect(names(a.facts, 'name')).toEqual(['Alice Calls Them Bobby']);
    // EntityRefs elsewhere use the same folded name.
    expect((await t.get(`/v0/entities?ids=${p}`, alice)).body[0].name).toBe('Alice Calls Them Bobby');
  });

  it('a retract hides a public fact for that viewer only', async () => {
    const p = uid('person', 'retract');
    const c = uid('company', 'retract');
    await t.write('public/test', [
      nameClaim(p, 'Retract Person'),
      nameClaim(c, 'Retract Co'),
      { subject: ref(p), predicate: 'headline', value: 'Public headline', source_ref: 'r:h' },
      { subject: ref(p), predicate: 'worked_at', object: ref(c), source_ref: 'r:w' },
    ]);
    await ownerClaim('user/alice/gmail', p, 'headline', null, { retract: true });
    await ownerClaim('user/alice/gmail', p, 'worked_at', null, { object: c, retract: true });

    const anon = (await t.get(`/v0/entities/${p}`)).body;
    expect(names(anon.facts, 'headline')).toEqual(['Public headline']);
    expect(anon.facts.filter((f: any) => f.predicate === 'worked_at')).toHaveLength(1);
    const a = (await t.get(`/v0/entities/${p}`, alice)).body;
    expect(names(a.facts, 'headline')).toEqual([]);
    expect(a.facts.filter((f: any) => f.predicate === 'worked_at')).toHaveLength(0);
    // Incoming side folds the same way.
    expect((await t.get(`/v0/entities/${c}`)).body.incoming).toHaveLength(1);
    expect((await t.get(`/v0/entities/${c}`, alice)).body.incoming).toHaveLength(0);
  });

  it('one keeps a single winner; many keeps every distinct value', async () => {
    const c = uid('company', 'card');
    await t.write('public/test', [nameClaim(c, 'Card Co'), { subject: ref(c), predicate: 'headcount', value: 10, source_ref: 'a' }]);
    await t.write('public/other', [
      { subject: ref(c), predicate: 'headcount', value: 25, source_ref: 'b' },
      { subject: ref(c), predicate: 'domain', value: 'card.example', source_ref: 'd1' },
      { subject: ref(c), predicate: 'domain', value: 'card2.example', source_ref: 'd2' },
    ]);
    await t.write('public/test', [{ subject: ref(c), predicate: 'domain', value: 'card.example', source_ref: 'd3' }]);
    const v = (await t.get(`/v0/entities/${c}`)).body;
    // Same scope: newest updated_at wins.
    expect(names(v.facts, 'headcount')).toEqual([25]);
    // Duplicate values across layers fold into one fact.
    expect(names(v.facts, 'domain').sort()).toEqual(['card.example', 'card2.example']);
  });

  it('within a scope, the newest write wins', async () => {
    const c = uid('company', 'newest');
    await t.write('public/other', [nameClaim(c, 'Old Name')]);
    await t.write('public/test', [nameClaim(c, 'New Name', 'test', false)]);
    expect((await t.get(`/v0/entities/${c}`)).body.name).toBe('New Name');
    await t.write('public/other', [{ ...nameClaim(c, 'Newest Name', 'test', false), source_ref: `test:${c}:name` }]);
    expect((await t.get(`/v0/entities/${c}`)).body.name).toBe('Newest Name');
  });
});

describe('merge follow', () => {
  let target: string;
  let old: string;
  let co: string;
  beforeAll(async () => {
    target = uid('company', 'mergetarget');
    old = uid('company', 'mergeold');
    co = uid('person', 'mergefounder');
    await t.write('public/test', [
      nameClaim(target, 'Zentropic Labs'),
      nameClaim(old, 'Zentropic Labs Inc'),
      nameClaim(co, 'Mergey Founder'),
      { subject: ref(old), predicate: 'domain', value: 'zentropic.example', source_ref: 'm:d' },
      { subject: ref(old), predicate: 'yc_batch', value: 'S99', source_ref: 'm:y' },
      { subject: ref(co), predicate: 'founded', object: ref(old), source_ref: 'm:f' },
    ]);
    await t.owner`update entity set merged_into = ${target} where id = ${old}`;
  });

  it('facts from the merged-away entity appear on the target', async () => {
    const v = (await t.get(`/v0/entities/${target}`)).body;
    expect(v.merged_from).toEqual([old]);
    expect(v.name).toBe('Zentropic Labs');
    expect(names(v.facts, 'domain')).toEqual(['zentropic.example']);
    expect(names(v.facts, 'yc_batch')).toEqual(['S99']);
    expect(v.incoming.map((f: any) => [f.predicate, f.subject.id, f.object.id])).toEqual([['founded', co, target]]);
    // The founder's outgoing edge points at the target.
    const f = (await t.get(`/v0/entities/${co}`)).body.facts.find((x: any) => x.predicate === 'founded');
    expect(f.object).toEqual({ id: target, kind: 'company', name: 'Zentropic Labs' });
  });

  it('the merged id redirects to the target view', async () => {
    const r = await t.get(`/v0/entities/${old}`);
    expect(r.status).toBe(200);
    expect(r.body.redirect).toBe(target);
    expect(r.body.entity.id).toBe(target);
    expect((await t.get(`/v0/entities?ids=${old},${target}`)).body.map((e: any) => e.id)).toEqual([target, target]);
  });

  it('search and query exclude merged entities', async () => {
    const hits = (await t.get('/v0/search?q=zentropic')).body.map((h: any) => h.id);
    expect(hits).toContain(target);
    expect(hits).not.toContain(old);
    const q = await t.post('/v0/query', { find: 'company', where: [{ attr: { predicate: 'yc_batch', eq: 'S99' } }] });
    expect(q.body.results.map((r: any) => r.id)).toEqual([target]);
    const q2 = await t.post('/v0/query', { find: 'company', where: [{ name: 'Zentropic Labs Inc' }] });
    // The old entity's name lost the fold on the target, so only the target's effective name counts.
    expect(q2.body.results).toEqual([]);
  });
});

describe('search', () => {
  beforeAll(async () => {
    await t.write('public/test', [
      nameClaim('rbx:company:search-stripe-press', 'Stripe Press'),
      nameClaim('rbx:company:search-stripe', 'Stripe'),
      nameClaim('rbx:company:search-stripes-r-us', 'Stripes R Us'),
      nameClaim('rbx:person:search-stripe-fan', 'Pat Stripe-Fan'),
    ]);
  });

  it('ranks "stripe" above "stripe press"', async () => {
    const r = await t.get('/v0/search?q=stripe');
    expect(r.status).toBe(200);
    const ids = r.body.map((h: any) => h.id);
    expect(ids[0]).toBe('rbx:company:search-stripe');
    expect(ids.indexOf('rbx:company:search-stripe')).toBeLessThan(ids.indexOf('rbx:company:search-stripe-press'));
    expect(r.body[0]).toEqual({ id: 'rbx:company:search-stripe', kind: 'company', name: 'Stripe', score: expect.any(Number) });
    expect(r.body[0].score).toBeGreaterThan(r.body[1].score);
  });

  it('filters by kind and limits', async () => {
    const r = await t.get('/v0/search?q=stripe&kind=person');
    expect(r.body.map((h: any) => h.id)).toEqual(['rbx:person:search-stripe-fan']);
    expect((await t.get('/v0/search?q=stripe&limit=1')).body).toHaveLength(1);
    expect((await t.get('/v0/search?q=stripe&limit=0')).status).toBe(400);
    expect((await t.get('/v0/search?q=')).body).toEqual([]);
  });
});

describe('edges endpoint', () => {
  it('pages through edges with an opaque cursor', async () => {
    const c = uid('company', 'hub');
    const people = Array.from({ length: 5 }, (_, i) => uid('person', `emp${i}`));
    await t.write('public/test', [
      nameClaim(c, 'Hub Co'),
      ...people.map((p, i) => nameClaim(p, `Employee ${i}`)),
      ...people.map((p) => ({ subject: ref(p), predicate: 'worked_at', object: ref(c), source_ref: `e:${p}` })),
    ]);
    const seen: string[] = [];
    let cursor: string | null = null;
    let pages = 0;
    do {
      const qs: string = `dir=in&predicate=worked_at&limit=2${cursor ? `&cursor=${cursor}` : ''}`;
      const r = await t.get(`/v0/entities/${c}/edges?${qs}`);
      expect(r.status).toBe(200);
      seen.push(...r.body.edges.map((e: any) => e.subject.id));
      cursor = r.body.next;
      pages += 1;
    } while (cursor && pages < 10);
    expect(pages).toBe(3);
    expect(seen.sort()).toEqual([...people].sort());
    const out = await t.get(`/v0/entities/${people[0]}/edges?dir=out`);
    expect(out.body.edges).toHaveLength(1);
    expect(out.body.edges[0].object.name).toBe('Hub Co');
    expect(out.body.next).toBeNull();
    expect((await t.get(`/v0/entities/${c}/edges?cursor=garbage`)).status).toBe(400);
    expect((await t.get(`/v0/entities/${c}/edges?predicate=name`)).status).toBe(400);
    expect((await t.get(`/v0/entities/rbx:company:nope/edges`)).status).toBe(404);
  });
});

describe('changes feed', () => {
  it('returns inserts in seq order and pages with next', async () => {
    const start = (await t.get('/v0/changes?after=0&limit=1000')).body;
    let after = start.next;
    // Drain anything newer than the first page.
    for (let r = start; r.changes.length === 1000; ) {
      r = (await t.get(`/v0/changes?after=${after}&limit=1000`)).body;
      after = r.next;
    }
    const ids = [uid('company', 'chg'), uid('company', 'chg'), uid('company', 'chg')];
    await t.write('public/test', ids.map((id, i) => nameClaim(id, `Change ${i}`)));
    const r = (await t.get(`/v0/changes?after=${after}&limit=500`)).body;
    expect(r.changes.map((c: any) => [c.op, c.subject, c.predicate])).toEqual(ids.map((id) => ['insert', id, 'name']));
    const seqs = r.changes.map((c: any) => c.seq);
    expect([...seqs].sort((a, b) => a - b)).toEqual(seqs);
    expect(r.next).toBe(seqs[2]);
    expect(Object.keys(r.changes[0]).sort()).toEqual(['at', 'layer_id', 'object_id', 'op', 'predicate', 'seq', 'subject']);
    const two = (await t.get(`/v0/changes?after=${after}&limit=2`)).body;
    expect(two.changes).toHaveLength(2);
    expect((await t.get(`/v0/changes?after=${two.next}`)).body.changes[0].subject).toBe(ids[2]);
    expect((await t.get(`/v0/changes?after=${r.next}`)).body).toEqual({ changes: [], next: r.next });
  });

  it('hides changes in layers the viewer cannot see', async () => {
    const p = uid('person', 'chgpriv');
    await t.write('public/test', [nameClaim(p, 'Chg Priv')]);
    const [{ s }] = (await t.owner`select max(seq)::int as s from change`) as unknown as [{ s: number }];
    await ownerClaim('user/alice/gmail', p, 'headline', 'x');
    expect((await t.get(`/v0/changes?after=${s}`)).body.changes).toEqual([]);
    expect((await t.get(`/v0/changes?after=${s}`, alice)).body.changes).toHaveLength(1);
  });
});
