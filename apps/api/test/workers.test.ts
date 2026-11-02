/** CONTRACTS §12/§14: workers, runs, evidence, external tokens, trust tie-break, support, stats. */
import { beforeAll, describe, expect, it } from 'vitest';
import { ADMIN, INGEST, TEST_WORKER, mint, nameClaim, ref, setupApp, uid } from './helpers.js';

const t = setupApp({ env: 'dev' });
const ingest = { authorization: `Bearer ${INGEST}` };
const admin = { authorization: `Bearer ${ADMIN}` };
const tag = `${process.pid}${Date.now().toString(36)}`.replace(/[^a-z0-9]/g, '');
const ev = (url = 'https://example.com/post/1') => [{ url, author: 'someone', quote: 'we raised' }];

/** POST /internal/claims with an explicit body (no helper defaults). */
const rawWrite = (body: Record<string, unknown>, headers: Record<string, string> = ingest) =>
  t.post('/internal/claims', body, headers);

async function register(body: Record<string, unknown>) {
  return t.post('/internal/workers', body, ingest);
}

describe('worker registration', () => {
  it('is idempotent on (name, kind, source) and mints <name>-<kind>-<6 digits>', async () => {
    const name = `reg${tag}`;
    const body = { name, kind: 'spider', source: 'x_spider', layers: ['public/x_spider'], config: { seeds: ['davidbooth'] } };
    const a = await register(body);
    expect(a.status).toBe(201);
    expect(a.body.id).toMatch(new RegExp(`^${name}-spider-\\d{6}$`));
    const b = await register(body);
    expect(b.status).toBe(200);
    expect(b.body.id).toBe(a.body.id);
    // Concurrent first registrations agree on one id.
    const name2 = `conc${tag}`;
    const many = await Promise.all(
      Array.from({ length: 5 }, () => register({ ...body, name: name2 })),
    );
    expect(new Set(many.map((r) => r.body.id)).size).toBe(1);
    const [row] = await t.owner`select kind, source, owner, trust, layers, config, status from worker where id = ${a.body.id}`;
    expect(row).toEqual({
      kind: 'spider', source: 'x_spider', owner: 'rebox', trust: 0.8, layers: ['public/x_spider'],
      config: { seeds: ['davidbooth'] }, status: 'active',
    });
    // Another source is another worker; re-registering with a new layer extends the layer list.
    expect((await register({ ...body, source: 'other' })).body.id).not.toBe(a.body.id);
    await register({ ...body, layers: ['public/x_spider', 'public/x_more'] });
    const [after] = await t.owner`select layers from worker where id = ${a.body.id}`;
    expect(after!.layers).toEqual(['public/x_spider', 'public/x_more']);
  });

  it('accepts an explicit id, applies default trust per kind, and validates', async () => {
    const id = `exp${tag}-parser-123456`;
    const r = await register({ id, name: `exp${tag}`, kind: 'parser', source: 'x_parse', layers: ['public/x_ai'] });
    expect(r.body).toEqual({ id, created: true });
    expect((await t.owner`select trust from worker where id = ${id}`)[0]!.trust).toBeCloseTo(0.6);
    expect((await register({ id, name: `exp${tag}`, kind: 'parser', source: 'x_parse', layers: ['public/x_ai'] })).body.id).toBe(id);
    expect((await register({ id: 'zzz-parser-123456', name: 'other', kind: 'parser', source: 's', layers: ['public/a'] })).status).toBe(400);
    expect((await register({ name: 'Bad Name', kind: 'connector', source: 's', layers: ['public/a'] })).status).toBe(400);
    expect((await register({ name: 'ok', kind: 'robot', source: 's', layers: ['public/a'] })).status).toBe(400);
    expect((await register({ name: 'ok', kind: 'external', source: 's', layers: ['public/a'] })).status).toBe(400);
    expect((await t.post('/internal/workers', { name: 'ok', kind: 'connector', source: 's', layers: ['public/a'] })).status).toBe(401);
    expect((await t.post('/internal/workers', { name: 'ok', kind: 'connector', source: 's', layers: ['public/a'] }, admin)).status).toBe(403);
  });
});

describe('writer provenance', () => {
  it('requires a worker; stamps worker_id, run_id and last_seen_at', async () => {
    const layer = `public/prov${tag}`;
    const p = uid('person', 'prov');
    expect((await rawWrite({ layer, claims: [nameClaim(p, 'P')] })).status).toBe(400);
    const w = await t.worker(layer, { name: `prov${tag}` });
    const run = await t.post('/internal/runs', { worker: w, kind: 'sync' }, ingest);
    expect(run.status).toBe(201);
    const runId = run.body.run_id as number;
    const res = await t.write(layer, [nameClaim(p, 'Prov Person')], { worker: w, run_id: runId });
    expect(res.body).toEqual({ written: 1, minted: 1 });
    const [c] = await t.owner`select worker_id, run_id::int as run_id, evidence from claim where subject = ${p}`;
    expect(c).toEqual({ worker_id: w, run_id: runId, evidence: null });
    const [ch] = await t.owner`select worker_id, run_id::int as run_id from change where subject = ${p}`;
    expect(ch).toEqual({ worker_id: w, run_id: runId });
    const [seen] = await t.owner`select last_seen_at > now() - interval '1 minute' as fresh from worker where id = ${w}`;
    expect(seen!.fresh).toBe(true);
    const fact = (await t.get(`/v0/entities/${p}`)).body.facts.find((f: any) => f.predicate === 'name');
    expect(fact).toMatchObject({ worker: w, evidence: null, support: 1 });
  });

  it('rejects unknown, paused and wrong-layer workers, and foreign runs (nothing written)', async () => {
    const layer = `public/perm${tag}`;
    const w = await t.worker(layer, { name: `perm${tag}` });
    const other = await t.worker(`public/perm2${tag}`, { name: `permtwo${tag}` });
    const otherRun = (await t.post('/internal/runs', { worker: other, kind: 'sync' }, ingest)).body.run_id;
    const before = (await t.owner`select count(*)::int as n from claim`)[0]!.n;
    const cases: [Record<string, unknown>, RegExp][] = [
      [{ layer, worker: 'nobody-connector-000000' }, /does not exist/],
      [{ layer: `public/elsewhere${tag}`, worker: w }, /may not write layer/],
      [{ layer, worker: w, run_id: otherRun }, /belongs to worker/],
      [{ layer, worker: w, run_id: 999999999 }, /run 999999999 does not exist/],
      [{ layer, worker: 'Not An Id' }, /worker id/],
    ];
    for (const [body, msg] of cases) {
      const r = await rawWrite({ ...body, claims: [nameClaim(uid('person'), 'X')] });
      expect(r.status, JSON.stringify(body)).toBe(400);
      expect(JSON.stringify(r.body)).toMatch(msg);
    }
    await t.owner`update worker set status = 'paused' where id = ${w}`;
    const paused = await rawWrite({ layer, worker: w, claims: [nameClaim(uid('person'), 'X')] });
    expect(paused.status).toBe(400);
    expect(JSON.stringify(paused.body)).toMatch(/paused, not active/);
    await t.owner`update worker set status = 'active' where id = ${w}`;
    expect((await t.owner`select count(*)::int as n from claim`)[0]!.n).toBe(before);
  });

  it('requires evidence for parser, enricher and external workers; stores and shows it', async () => {
    const layer = `public/ai${tag}`;
    const parser = await t.worker(layer, { name: `xbio${tag}`, kind: 'parser' });
    const connector = await t.worker(layer, { name: `conn${tag}` });
    const p = uid('person', 'ev');
    const c = uid('company', 'ev');
    const claims = [nameClaim(p, 'Ev Person'), nameClaim(c, 'Ev Co'), { subject: ref(p), predicate: 'worked_at', object: ref(c), source_ref: 'ev:1' }];
    const no = await t.write(layer, claims, { worker: parser });
    expect(no.status).toBe(400);
    expect(no.body.details.map((d: any) => d.index)).toEqual([0, 1, 2]);
    expect(no.body.details[0].message).toMatch(/evidence is required for parser workers/);
    // Connectors may write without evidence.
    expect((await t.write(layer, [nameClaim(uid('person'), 'No Ev')], { worker: connector })).status).toBe(200);
    const withEv = claims.map((x, i) => ({ ...x, evidence: ev(`https://x.com/a/status/${i}`) }));
    expect((await t.write(layer, withEv, { worker: parser })).body).toEqual({ written: 3, minted: 2 });
    const f = (await t.get(`/v0/entities/${p}`)).body.facts.find((x: any) => x.predicate === 'worked_at');
    expect(f.evidence).toEqual([{ url: 'https://x.com/a/status/2', author: 'someone', quote: 'we raised' }]);
    expect(f.worker).toBe(parser);
    // Unchanged re-send is a no-op; changed evidence is an update.
    expect((await t.write(layer, withEv, { worker: parser })).body.written).toBe(0);
    const changed = [{ ...withEv[2]!, evidence: ev('https://x.com/a/status/99') }];
    expect((await t.write(layer, changed, { worker: parser })).body.written).toBe(1);
    // Evidence shape is validated.
    const bad = await t.write(layer, [{ ...withEv[0]!, evidence: [{ url: 'javascript:alert(1)' }] }], { worker: parser });
    expect(bad.status).toBe(400);
    expect((await t.write(layer, [{ ...withEv[0]!, evidence: [{ url: 'https://a.example', extra: 1 }] }], { worker: parser })).status).toBe(400);
  });
});

describe('json-valued attributes and any-kind edges', () => {
  it('accepts an object for value_type json (milestone) and rejects it elsewhere', async () => {
    const c = uid('company', 'ms');
    const ms = { title: 'Raised a $4M seed', kind: 'funding', url: 'https://example.com/seed' };
    const r = await t.write('public/test', [
      nameClaim(c, 'Milestone Co'),
      { subject: ref(c), predicate: 'milestone', value: ms, valid_from: '2026-03-01', source_ref: `ms:${c}:1` },
    ]);
    expect(r.status).toBe(200);
    const facts = (await t.get(`/v0/entities/${c}`)).body.facts;
    expect(facts.find((f: any) => f.predicate === 'milestone')).toMatchObject({ value: ms, valid_from: '2026-03-01T00:00:00.000Z' });
    const notObj = await t.write('public/test', [{ subject: ref(c), predicate: 'milestone', value: 'raised', source_ref: 'ms:x' }]);
    expect(notObj.status).toBe(400);
    expect(JSON.stringify(notObj.body)).toMatch(/must be a JSON object/);
    const arr = await t.write('public/test', [{ subject: ref(c), predicate: 'milestone', value: [ms], source_ref: 'ms:y' }]);
    expect(arr.status).toBe(400);
    const objForText = await t.write('public/test', [{ subject: ref(c), predicate: 'description', value: { a: 1 }, source_ref: 'd:x' }]);
    expect(objForText.status).toBe(400);
    expect(JSON.stringify(objForText.body)).toMatch(/scalar or an array of scalars/);
  });

  it("object_kind '*' (follows) accepts any entity kind", async () => {
    const p = uid('person', 'fol');
    const c = uid('company', 'fol');
    const r = await t.write('public/test', [
      nameClaim(p, 'Follower'),
      nameClaim(c, 'Followed Co'),
      { subject: ref(p), predicate: 'follows', object: ref(c), source_ref: `f:${p}:${c}` },
      { subject: ref(p), predicate: 'follows', object: mint(uid('person', 'fol2')), source_ref: `f:${p}:2` },
    ]);
    expect(r.status).toBe(200);
  });
});

describe('runs', () => {
  it('opens, merges stats and closes with duration', async () => {
    const w = await t.worker(`public/runs${tag}`, { name: `runs${tag}` });
    const { run_id } = (await t.post('/internal/runs', { worker: w, kind: 'sync' }, ingest)).body;
    const p1 = await t.patch(`/internal/runs/${run_id}`, { stats: { fetched: 10, claims: 5 } }, ingest);
    expect(p1.body).toMatchObject({ status: 'running', stats: { fetched: 10, claims: 5 }, finished_at: null });
    const p2 = await t.patch(`/internal/runs/${run_id}`, { status: 'failed', stats: { claims: 7, cost_usd: 0.12 }, error: 'boom' }, ingest);
    expect(p2.body).toMatchObject({ status: 'failed', stats: { fetched: 10, claims: 7, cost_usd: 0.12 }, error: 'boom' });
    expect(p2.body.finished_at).not.toBeNull();
    expect(p2.body.duration_s).toBeGreaterThanOrEqual(0);
    expect((await t.patch(`/internal/runs/${run_id}`, { status: 'running' }, ingest)).status).toBe(400);
    // Connector-reported stats may be any JSON (lists, strings), within a size cap.
    const fq = await t.patch(`/internal/runs/${run_id}`, { stats: { failed_queries: ['bad'], x_stop: '429' } }, ingest);
    expect(fq.body.stats).toMatchObject({ failed_queries: ['bad'], x_stop: '429', fetched: 10 });
    expect((await t.patch(`/internal/runs/${run_id}`, { stats: { big: 'x'.repeat(40_000) } }, ingest)).status).toBe(400);
    expect((await t.patch(`/internal/runs/999999999`, { status: 'ok' }, ingest)).status).toBe(404);
    expect((await t.post('/internal/runs', { worker: w, kind: 'dance' }, ingest)).status).toBe(400);
    expect((await t.post('/internal/runs', { worker: 'ghost-connector-000000', kind: 'sync' }, ingest)).status).toBe(404);
    expect((await t.patch(`/internal/runs/${run_id}`, { status: 'ok' })).status).toBe(401);
  });
});

describe('external workers', () => {
  let ext: { id: string; token: string };
  const layer = `public/ext${tag}`;
  beforeAll(async () => {
    const r = await t.post('/v0/admin/workers', { name: `acme${tag}`, owner: 'acme-labs', source: 'acme', layers: [layer], description: 'Acme contributor' }, admin);
    expect(r.status).toBe(201);
    ext = r.body;
  });

  it('issues a one-time token, stores only its hash, defaults trust 0.3', async () => {
    expect(ext.id).toMatch(new RegExp(`^acme${tag}-external-\\d{6}$`));
    expect(ext.token).toMatch(new RegExp(`^rbxw_${ext.id}_[0-9a-f]{32}$`));
    const [row] = await t.owner`select token_hash, trust, owner, kind from worker where id = ${ext.id}`;
    expect(row!.token_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(row!.token_hash).not.toContain(ext.token.slice(-32));
    expect(row).toMatchObject({ owner: 'acme-labs', kind: 'external' });
    expect(row!.trust).toBeCloseTo(0.3);
    // The token never appears in reads.
    expect(JSON.stringify((await t.get(`/v0/workers/${ext.id}`)).body)).not.toContain('rbxw_');
    expect((await t.post('/v0/admin/workers', { name: 'x', owner: 'rebox', source: 's', layers: [layer] }, admin)).status).toBe(400);
    expect((await t.post('/v0/admin/workers', { name: 'x', owner: 'o', source: 's', layers: [layer] }, ingest)).status).toBe(403);
  });

  it('writes only as itself, with evidence; ingest cannot act as it', async () => {
    const auth = { authorization: `Bearer ${ext.token}` };
    const p = uid('person', 'ext');
    const claim = { ...nameClaim(p, 'Ext Person'), evidence: ev() };
    const ok = await rawWrite({ layer, worker: ext.id, claims: [claim] }, auth);
    expect(ok.status).toBe(200);
    expect((await rawWrite({ layer, worker: ext.id, claims: [nameClaim(uid('person'), 'No Ev')] }, auth)).status).toBe(400);
    expect((await rawWrite({ layer, worker: TEST_WORKER, claims: [claim] }, auth)).status).toBe(403);
    expect((await rawWrite({ layer, worker: ext.id, claims: [claim] }, ingest)).status).toBe(403);
    // External tokens open runs for themselves only, and are not admin or ingest.
    const run = await t.post('/internal/runs', { worker: ext.id, kind: 'enrich' }, auth);
    expect(run.status).toBe(201);
    expect((await t.post('/internal/runs', { worker: TEST_WORKER, kind: 'sync' }, auth)).status).toBe(403);
    expect((await t.get('/v0/review', auth)).status).toBe(403);
    expect((await t.post('/internal/workers', { name: 'x', kind: 'connector', source: 's', layers: [layer] }, auth)).status).toBe(403);
  });

  it('rejects wrong secrets, unknown ids and revoked tokens (401)', async () => {
    const claim = { ...nameClaim(uid('person'), 'Rev'), evidence: ev() };
    const wrong = ext.token.slice(0, -4) + (ext.token.endsWith('0000') ? '1111' : '0000');
    expect((await rawWrite({ layer, worker: ext.id, claims: [claim] }, { authorization: `Bearer ${wrong}` })).status).toBe(401);
    const ghost = `rbxw_ghost-external-000000_${'a'.repeat(32)}`;
    expect((await rawWrite({ layer, worker: ext.id, claims: [claim] }, { authorization: `Bearer ${ghost}` })).status).toBe(401);
    expect((await t.get('/v0/stats', { authorization: 'Bearer rbxw_junk' })).status).toBe(401);
    const revoked = await t.patch(`/v0/admin/workers/${ext.id}`, { status: 'revoked' }, admin);
    expect(revoked.body.status).toBe('revoked');
    expect((await rawWrite({ layer, worker: ext.id, claims: [claim] }, { authorization: `Bearer ${ext.token}` })).status).toBe(401);
  });
});

describe('fold: trust tie-break and support', () => {
  it('within a scope, higher trust beats a newer write; scope still beats trust', async () => {
    const c = uid('company', 'trust');
    const hi = await t.worker(`public/hi${tag}`, { name: `hi${tag}` }); // connector 0.8
    const lo = await t.worker(`public/lo${tag}`, { name: `lo${tag}`, kind: 'parser' }); // parser 0.6
    await t.write(`public/hi${tag}`, [nameClaim(c, 'Trusted Name'), { subject: ref(c), predicate: 'headcount', value: 40, source_ref: 'h' }], { worker: hi });
    await t.write(
      `public/lo${tag}`,
      [{ ...nameClaim(c, 'Newer Untrusted', 'lo', false), evidence: ev() }, { subject: ref(c), predicate: 'headcount', value: 41, source_ref: 'h', evidence: ev() }],
      { worker: lo },
    );
    let v = (await t.get(`/v0/entities/${c}`)).body;
    expect(v.name).toBe('Trusted Name');
    expect(v.facts.find((f: any) => f.predicate === 'headcount')).toMatchObject({ value: 40, worker: hi, support: 1 });
    // Lowering the connector's trust flips the winner to the newer claim.
    await t.patch(`/v0/admin/workers/${hi}`, { trust: 0.1 }, admin);
    v = (await t.get(`/v0/entities/${c}`)).body;
    expect(v.name).toBe('Newer Untrusted');
    await t.patch(`/v0/admin/workers/${hi}`, { trust: 0.8 }, admin);
    // A user layer (narrower scope) wins regardless of trust, for that viewer only.
    await t.owner`insert into layer (id, scope, owner, source) values ('user/alice/notes', 'user', 'alice', 'notes') on conflict do nothing`;
    await t.owner`insert into claim (layer_id, subject, predicate, value, source_ref) values ('user/alice/notes', ${c}, 'name', '"Alice Name"', 'a')`;
    expect((await t.get(`/v0/entities/${c}`, { 'x-rebox-viewer': 'alice' })).body.name).toBe('Alice Name');
    expect((await t.get(`/v0/entities/${c}`)).body.name).toBe('Trusted Name');
  });

  it('support counts distinct (layer, source_ref) behind the same fact, within visible layers', async () => {
    const p = uid('person', 'sup');
    const c = uid('company', 'sup');
    const a = await t.worker(`public/supa${tag}`, { name: `supa${tag}` });
    const b = await t.worker(`public/supb${tag}`, { name: `supb${tag}` });
    const edge = { subject: ref(p), predicate: 'worked_at', object: ref(c) };
    await t.write(`public/supa${tag}`, [nameClaim(p, 'Sup Person'), nameClaim(c, 'Sup Co'), { ...edge, source_ref: 'a:1' }, { ...edge, source_ref: 'a:2' },
      { subject: ref(p), predicate: 'location', value: 'San Francisco', source_ref: 'loc' }], { worker: a });
    await t.write(`public/supb${tag}`, [{ ...edge, source_ref: 'b:1' }, { subject: ref(p), predicate: 'location', value: 'Oakland', source_ref: 'loc' },
      { subject: ref(p), predicate: 'name', value: 'Sup Person', source_ref: 'n' }], { worker: b });
    const facts = (await t.get(`/v0/entities/${p}`)).body.facts;
    expect(facts.filter((f: any) => f.predicate === 'worked_at')).toHaveLength(1);
    expect(facts.find((f: any) => f.predicate === 'worked_at').support).toBe(3);
    // `one` attribute: only claims agreeing with the winning value count.
    expect(facts.find((f: any) => f.predicate === 'name').support).toBe(2);
    expect(facts.find((f: any) => f.predicate === 'location').support).toBe(1);
    // Incoming side and edges endpoint carry support too.
    expect((await t.get(`/v0/entities/${c}`)).body.incoming[0].support).toBe(3);
    expect((await t.get(`/v0/entities/${c}/edges?dir=in`)).body.edges[0].support).toBe(3);
    // A private corroboration counts only for its viewer; a retracted claim never counts.
    await t.owner`insert into claim (layer_id, subject, predicate, object_id, source_ref) values ('user/alice/notes', ${p}, 'worked_at', ${c}, 'priv')`;
    expect((await t.get(`/v0/entities/${p}`, { 'x-rebox-viewer': 'alice' })).body.facts.find((f: any) => f.predicate === 'worked_at').support).toBe(4);
    expect((await t.get(`/v0/entities/${p}`)).body.facts.find((f: any) => f.predicate === 'worked_at').support).toBe(3);
    // A retract that loses the fold (lower trust) hides nothing, and does not count as support.
    await t.patch(`/v0/admin/workers/${b}`, { trust: 0.4 }, admin);
    await t.write(`public/supb${tag}`, [{ ...edge, source_ref: 'b:1', retract: true }], { worker: b });
    expect((await t.get(`/v0/entities/${p}`)).body.facts.find((f: any) => f.predicate === 'worked_at').support).toBe(2);
  });
});

describe('worker stats API', () => {
  let w: string;
  const layer = `public/stats${tag}`;
  beforeAll(async () => {
    w = await t.worker(layer, { name: `stats${tag}` });
    const run = (await t.post('/internal/runs', { worker: w, kind: 'sync' }, ingest)).body.run_id;
    const people = Array.from({ length: 3 }, (_, i) => uid('person', `st${i}`));
    const co = uid('company', 'st');
    await t.write(layer, [nameClaim(co, 'Stats Inc'), ...people.map((p, i) => nameClaim(p, `Stat ${i}`)),
      ...people.map((p) => ({ subject: ref(p), predicate: 'worked_at', object: ref(co), source_ref: `w:${p}` }))], { worker: w, run_id: run });
    await t.patch(`/internal/runs/${run}`, { status: 'ok', stats: { fetched: 3, claims: 7, written: 7 } }, ingest);
    const failed = (await t.post('/internal/runs', { worker: w, kind: 'replay' }, ingest)).body.run_id;
    await t.patch(`/internal/runs/${failed}`, { status: 'failed', error: 'ValueError: bad record' }, ingest);
  });

  it('GET /v0/workers lists claims, runs and the last run', async () => {
    const r = await t.get('/v0/workers');
    expect(r.status).toBe(200);
    const row = r.body.find((x: any) => x.id === w);
    expect(row).toMatchObject({ kind: 'connector', source: `stats${tag}`, owner: 'rebox', trust: 0.8, status: 'active',
      claims_total: 7, claims_24h: 7, runs_ok: 1, runs_failed: 1 });
    expect(row.last_run).toMatchObject({ kind: 'replay', status: 'failed', error: 'ValueError: bad record' });
    expect(Object.keys(row).sort()).toEqual(['claims_24h', 'claims_total', 'description', 'id', 'kind', 'last_run', 'last_seen_at', 'owner', 'runs_failed', 'runs_ok', 'source', 'status', 'trust']);
  });

  it('GET /v0/workers/:id has runs, a 48h throughput series, predicates and recent facts', async () => {
    const r = await t.get(`/v0/workers/${w}`);
    expect(r.status).toBe(200);
    expect(r.body.runs.map((x: any) => [x.kind, x.status])).toEqual([['replay', 'failed'], ['sync', 'ok']]);
    expect(r.body.runs[1].stats).toEqual({ fetched: 3, claims: 7, written: 7 });
    expect(r.body.throughput).toHaveLength(48);
    expect(r.body.throughput.at(-1).n).toBe(7);
    expect(r.body.throughput.reduce((n: number, x: any) => n + x.n, 0)).toBe(7);
    expect(r.body.predicates).toEqual([{ predicate: 'name', n: 4 }, { predicate: 'worked_at', n: 3 }]);
    expect(r.body.recent_facts).toHaveLength(7);
    expect(r.body.recent_facts[0]).toMatchObject({ worker: w, layer, support: 1 });
    expect(r.body.recent_facts.find((f: any) => f.predicate === 'worked_at').object.name).toBe('Stats Inc');
    expect((await t.get('/v0/workers/nobody-connector-000000')).status).toBe(404);
  });

  it('GET /v0/stats/timeseries buckets claims and first-seen entities', async () => {
    const tsLayer = `public/ts${tag}`;
    const tw = await t.worker(tsLayer, { name: `ts${tag}` });
    const ids = Array.from({ length: 4 }, (_, i) => uid(i < 3 ? 'person' : 'company', `ts${i}`));
    await t.write(tsLayer, ids.map((id) => nameClaim(id, `TS ${id}`)), { worker: tw });
    await t.write(tsLayer, [{ subject: ref(ids[0]!), predicate: 'headline', value: 'later', source_ref: 'h' }], { worker: tw });
    // Pin the change rows to known hours, 3 days ago.
    const [{ base }] = (await t.owner`select date_trunc('hour', now()) - interval '3 days' as base`) as unknown as [{ base: Date }];
    await t.owner`update change set at = ${base} where layer_id = ${tsLayer} and predicate = 'name' and subject = any(${ids.slice(0, 2)})`;
    await t.owner`update change set at = ${base}::timestamptz + interval '1 hour' where layer_id = ${tsLayer} and predicate = 'name' and subject = any(${ids.slice(2)})`;
    await t.owner`update change set at = ${base}::timestamptz + interval '2 hours' where layer_id = ${tsLayer} and predicate = 'headline'`;
    const h0 = base.toISOString();
    const h1 = new Date(base.getTime() + 3600e3).toISOString();
    const h2 = new Date(base.getTime() + 7200e3).toISOString();
    const since = encodeURIComponent(h0);

    const claims = await t.get(`/v0/stats/timeseries?metric=claims&by=worker&bucket=hour&since=${since}`);
    expect(claims.status).toBe(200);
    expect(claims.body.buckets.filter((b: any) => b.key === tw)).toEqual([
      { t: h0, key: tw, n: 2 }, { t: h1, key: tw, n: 2 }, { t: h2, key: tw, n: 1 },
    ]);
    const ents = await t.get(`/v0/stats/timeseries?metric=entities&by=layer&bucket=hour&since=${since}`);
    expect(ents.body.buckets.filter((b: any) => b.key === tsLayer)).toEqual([
      { t: h0, key: tsLayer, n: 2 }, { t: h1, key: tsLayer, n: 2 },
    ]);
    const byKind = await t.get(`/v0/stats/timeseries?metric=entities&by=kind&bucket=day&since=${since}`);
    const day = `${h0.slice(0, 10)}T00:00:00.000Z`;
    const kinds = byKind.body.buckets.filter((b: any) => b.t === day);
    expect(kinds.find((b: any) => b.key === 'person').n).toBeGreaterThanOrEqual(3);
    expect(kinds.find((b: any) => b.key === 'company').n).toBeGreaterThanOrEqual(1);

    expect((await t.get('/v0/stats/timeseries?metric=nope')).status).toBe(400);
    expect((await t.get('/v0/stats/timeseries?by=nope')).status).toBe(400);
    expect((await t.get('/v0/stats/timeseries?bucket=week')).status).toBe(400);
    expect((await t.get('/v0/stats/timeseries?since=yesterday')).status).toBe(400);
    expect((await t.get('/v0/stats/timeseries?bucket=hour&since=2001-01-01')).status).toBe(400);
    expect((await t.get('/v0/stats/timeseries')).status).toBe(200);
  });
});
