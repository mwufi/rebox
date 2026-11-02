import { beforeAll, describe, expect, it } from 'vitest';
import { ADMIN, INGEST, nameClaim, ref, setupApp, uid } from './helpers.js';

const t = setupApp();
const admin = { authorization: `Bearer ${ADMIN}` };

async function openReview(ids: string[], reason = 'same name, different domains') {
  const [row] = await t.owner<{ id: string }[]>`
    insert into review_item (kind, entity_ids, reason, evidence)
    values ('possible_duplicate', ${ids}, ${reason}, ${t.owner.json([{ key: 'name' }])})
    returning id::text as id`;
  return Number(row!.id);
}

async function mergedInto(id: string) {
  const [r] = await t.owner<{ merged_into: string | null }[]>`select merged_into from entity where id = ${id}`;
  return r?.merged_into ?? null;
}

describe('admin auth', () => {
  it('requires the admin token on every admin endpoint', async () => {
    const calls = [
      () => t.get('/v0/review'),
      () => t.post('/v0/admin/merge', { from: 'rbx:company:a', into: 'rbx:company:b' }),
      () => t.post('/v0/admin/split', { id: 'rbx:company:a' }),
      () => t.post('/v0/admin/review/1/dismiss', {}),
    ];
    for (const call of calls) {
      expect((await call()).status).toBe(401);
    }
    expect((await t.get('/v0/review', { authorization: 'Bearer wrong' })).status).toBe(401);
    expect((await t.get('/v0/review', { authorization: `Bearer ${INGEST}` })).status).toBe(403);
    expect((await t.post('/v0/admin/merge', { from: 'rbx:company:a', into: 'rbx:company:b' }, { authorization: `Bearer ${INGEST}` })).status).toBe(403);
    expect((await t.get('/v0/review', admin)).status).toBe(200);
  });
});

describe('merge', () => {
  let target: string;
  let dupe: string;
  let founder: string;
  let reviewId: number;
  beforeAll(async () => {
    target = uid('company', 'quillwork');
    dupe = uid('company', 'quillworkinc');
    founder = uid('person', 'founder');
    const r = await t.write('public/admintest', [
      nameClaim(target, 'Quillwork'),
      nameClaim(dupe, 'Quillwork Inc'),
      nameClaim(founder, 'Odile Fernsby'),
      { subject: ref(dupe), predicate: 'yc_batch', value: 'W31', source_ref: `adm:${dupe}:yc` },
      { subject: ref(founder), predicate: 'founded', object: ref(dupe), source_ref: `adm:${founder}:founded` },
    ]);
    expect(r.status).toBe(200);
    reviewId = await openReview([target, dupe]);
  });

  it('lists open review items with candidate EntityRefs', async () => {
    const r = await t.get('/v0/review?status=open', admin);
    expect(r.status).toBe(200);
    const item = r.body.find((i: any) => i.id === reviewId);
    expect(item).toMatchObject({
      kind: 'possible_duplicate',
      entity_ids: [target, dupe],
      reason: 'same name, different domains',
      status: 'open',
      resolved_by: null,
      candidates: [
        { id: target, kind: 'company', name: 'Quillwork' },
        { id: dupe, kind: 'company', name: 'Quillwork Inc' },
      ],
    });
    expect((await t.get('/v0/review?status=nope', admin)).status).toBe(400);
  });

  it('validates the body and refuses self-merges and unknown entities', async () => {
    expect((await t.post('/v0/admin/merge', { from: target }, admin)).status).toBe(400);
    expect((await t.post('/v0/admin/merge', { from: target, into: target }, admin)).status).toBe(400);
    expect((await t.post('/v0/admin/merge', { from: 'rbx:company:missing-x', into: target }, admin)).status).toBe(404);
    expect((await t.post('/v0/admin/merge', { from: founder, into: target }, admin)).body.details[0].message).toMatch(/cannot merge a person into a company/);
    expect(await mergedInto(target)).toBeNull();
  });

  it('merges, logs, resolves the review item and emits a change', async () => {
    const before = await t.owner<{ s: number }[]>`select coalesce(max(seq), 0)::int as s from change`;
    const r = await t.post('/v0/admin/merge', { from: dupe, into: target, review_id: reviewId }, admin);
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ merged: { from: dupe, into: target }, review_id: reviewId });
    expect(await mergedInto(dupe)).toBe(target);

    const log = await t.owner`select op, from_id, into_id, actor from merge_log where from_id = ${dupe}`;
    expect(log).toEqual([{ op: 'merge', from_id: dupe, into_id: target, actor: 'admin' }]);
    const [item] = await t.owner`select status, resolved_by, resolved_at is not null as resolved from review_item where id = ${reviewId}`;
    expect(item).toEqual({ status: 'merged', resolved_by: 'admin', resolved: true });

    const changes = (await t.get(`/v0/changes?after=${before[0]!.s}`)).body.changes;
    expect(changes).toEqual([expect.objectContaining({ seq: r.body.seq, op: 'merge', layer_id: null, subject: dupe, object_id: target })]);

    // The resolved item leaves the open list.
    const open = (await t.get('/v0/review', admin)).body.map((i: any) => i.id);
    expect(open).not.toContain(reviewId);
    const merged = (await t.get('/v0/review?status=merged', admin)).body.map((i: any) => i.id);
    expect(merged).toContain(reviewId);
  });

  it('reads follow the merge', async () => {
    const v = (await t.get(`/v0/entities/${target}`)).body;
    expect(v.merged_from).toEqual([dupe]);
    expect(v.facts.find((f: any) => f.predicate === 'yc_batch').value).toBe('W31');
    expect(v.incoming.map((f: any) => [f.predicate, f.subject.id])).toEqual([['founded', founder]]);

    const redirect = (await t.get(`/v0/entities/${dupe}`)).body;
    expect(redirect.redirect).toBe(target);
    expect(redirect.entity.id).toBe(target);

    const founderView = (await t.get(`/v0/entities/${founder}`)).body;
    expect(founderView.facts.find((f: any) => f.predicate === 'founded').object).toEqual({ id: target, kind: 'company', name: 'Quillwork' });

    const hits = (await t.get('/v0/search?q=quillwork')).body.map((h: any) => h.id);
    expect(hits).toContain(target);
    expect(hits).not.toContain(dupe);
    const q = await t.post('/v0/query', { find: 'company', where: [{ attr: { predicate: 'yc_batch', eq: 'W31' } }] });
    expect(q.body.results.map((x: any) => x.id)).toEqual([target]);
  });

  it('refuses to merge into a merged entity, re-merge, or resolve a closed review', async () => {
    const third = uid('company', 'quillthird');
    await t.write('public/admintest', [nameClaim(third, 'Quillwork Labs')]);
    // into is itself merged (would hang a chain off a non-root)
    const r1 = await t.post('/v0/admin/merge', { from: third, into: dupe }, admin);
    expect(r1.status).toBe(400);
    expect(r1.body.details[0].message).toMatch(/itself merged/);
    // from already merged
    const r2 = await t.post('/v0/admin/merge', { from: dupe, into: third }, admin);
    expect(r2.status).toBe(400);
    expect(r2.body.details[0].message).toMatch(/already merged/);
    // the target cannot be merged into its own merged-away member (cycle)
    const r3 = await t.post('/v0/admin/merge', { from: target, into: dupe }, admin);
    expect(r3.status).toBe(400);
    // review already resolved
    const r4 = await t.post('/v0/admin/merge', { from: third, into: target, review_id: reviewId }, admin);
    expect(r4.status).toBe(400);
    expect(await mergedInto(third)).toBeNull();
    expect(await mergedInto(target)).toBeNull();
    expect((await t.post('/v0/admin/merge', { from: third, into: target, review_id: 999999999 }, admin)).status).toBe(404);
  });

  it('allows chains through roots: merging a root that has members keeps them reachable', async () => {
    const top = uid('company', 'quilltop');
    await t.write('public/admintest', [nameClaim(top, 'Quillwork Holdings')]);
    expect((await t.post('/v0/admin/merge', { from: target, into: top }, admin)).status).toBe(200);
    const v = (await t.get(`/v0/entities/${dupe}`)).body;
    expect(v.redirect).toBe(top);
    expect(v.entity.merged_from).toEqual([target, dupe].sort());
    expect(v.entity.facts.find((f: any) => f.predicate === 'yc_batch').value).toBe('W31');
    // undo so later tests see the original shape
    expect((await t.post('/v0/admin/split', { id: target }, admin)).status).toBe(200);
  });
});

describe('split', () => {
  it('clears merged_into, logs, emits a change, and reads separate again', async () => {
    const a = uid('company', 'splita');
    const b = uid('company', 'splitb');
    await t.write('public/admintest', [
      nameClaim(a, 'Larkspur Robotics'),
      nameClaim(b, 'Larkspur Robotics Co'),
      { subject: ref(b), predicate: 'founded_year', value: 2031, source_ref: `adm:${b}:fy` },
    ]);
    expect((await t.post('/v0/admin/merge', { from: b, into: a }, admin)).status).toBe(200);
    expect((await t.get(`/v0/entities/${a}`)).body.facts.some((f: any) => f.predicate === 'founded_year')).toBe(true);

    const r = await t.post('/v0/admin/split', { id: b }, admin);
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ split: { id: b, from: a } });
    expect(await mergedInto(b)).toBeNull();
    const log = await t.owner`select op, from_id, into_id from merge_log where from_id = ${b} order by id`;
    expect(log).toEqual([
      { op: 'merge', from_id: b, into_id: a },
      { op: 'split', from_id: b, into_id: a },
    ]);
    const [ch] = await t.owner`select op, layer_id, subject, object_id from change where seq = ${r.body.seq}`;
    expect(ch).toEqual({ op: 'split', layer_id: null, subject: b, object_id: a });

    const av = (await t.get(`/v0/entities/${a}`)).body;
    expect(av.merged_from).toEqual([]);
    expect(av.facts.some((f: any) => f.predicate === 'founded_year')).toBe(false);
    const bv = (await t.get(`/v0/entities/${b}`)).body;
    expect(bv.redirect).toBeUndefined();
    expect(bv.id).toBe(b);
    expect((await t.get('/v0/search?q=larkspur')).body.map((h: any) => h.id).sort()).toEqual([a, b].sort());

    // Splitting an un-merged or unknown entity is refused.
    expect((await t.post('/v0/admin/split', { id: b }, admin)).status).toBe(400);
    expect((await t.post('/v0/admin/split', { id: 'rbx:company:nope-x' }, admin)).status).toBe(404);
    expect((await t.post('/v0/admin/split', {}, admin)).status).toBe(400);
  });
});

describe('dismiss', () => {
  it('marks an open item dismissed, once', async () => {
    const a = uid('person', 'dismissa');
    const b = uid('person', 'dismissb');
    await t.write('public/admintest', [nameClaim(a, 'Petra Vool'), nameClaim(b, 'Petra Vool')]);
    const id = await openReview([a, b], 'ambiguous hn_user');
    const r = await t.post(`/v0/admin/review/${id}/dismiss`, {}, admin);
    expect(r.status).toBe(200);
    expect(r.body).toMatchObject({ id, status: 'dismissed', resolved_by: 'admin', candidates: [{ id: a }, { id: b }] });
    expect(r.body.resolved_at).toEqual(expect.any(String));
    expect((await t.post(`/v0/admin/review/${id}/dismiss`, {}, admin)).status).toBe(400);
    expect((await t.post('/v0/admin/review/999999999/dismiss', {}, admin)).status).toBe(404);
    expect((await t.post('/v0/admin/review/abc/dismiss', {}, admin)).status).toBe(400);
    expect(await mergedInto(a)).toBeNull();
    expect(await mergedInto(b)).toBeNull();
    expect((await t.get('/v0/review', admin)).body.map((i: any) => i.id)).not.toContain(id);
  });
});
