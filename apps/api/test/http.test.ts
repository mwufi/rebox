import { mkdtempSync, writeFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { nameClaim, setupApp, uid } from './helpers.js';

const dir = mkdtempSync(join(tmpdir(), 'rebox-public-'));
writeFileSync(join(dir, 'index.html'), '<!doctype html><title>explorer</title>');
mkdirSync(join(dir, 'assets'));
writeFileSync(join(dir, 'assets', 'app.js'), 'console.log(1)');
const t = setupApp({ publicDir: dir });

describe('ops and static', () => {
  it('GET /health', async () => {
    const r = await t.get('/health');
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ ok: true, db: true });
  });

  it('sets X-Request-Id, echoes a valid one, and logs JSON lines', async () => {
    const r = await t.get('/health');
    expect(r.headers.get('x-request-id')).toMatch(/^[0-9a-f-]{36}$/);
    const echoed = await t.get('/health', { 'x-request-id': 'abc-123' });
    expect(echoed.headers.get('x-request-id')).toBe('abc-123');
    const line = t.logs.find((l) => l.req_id === 'abc-123');
    expect(line).toMatchObject({ method: 'GET', path: '/health', status: 200, ms: expect.any(Number) });
  });

  it('CORS * on GET /v0/* only', async () => {
    const r = await t.get('/v0/predicates', { origin: 'https://example.com' });
    expect(r.headers.get('access-control-allow-origin')).toBe('*');
    const w = await t.post('/internal/claims', {}, { origin: 'https://example.com' });
    expect(w.headers.get('access-control-allow-origin')).toBeNull();
    const q = await t.post('/v0/query', { find: 'person' }, { origin: 'https://example.com' });
    expect(q.headers.get('access-control-allow-origin')).toBeNull();
  });

  it('GET /v0/predicates and /v0/stats', async () => {
    const p = await t.get('/v0/predicates');
    const worked = p.body.find((x: any) => x.id === 'worked_at');
    expect(worked).toEqual({ id: 'worked_at', subject_kinds: ['person'], object_kind: 'company', cardinality: 'many', meta: expect.objectContaining({ filterable: true }) });
    await t.write('public/test', [nameClaim(uid('company'), 'Stats Co')]);
    const s = await t.get('/v0/stats');
    expect(s.status).toBe(200);
    expect(s.body.entities.company).toBeGreaterThan(0);
    expect(s.body.claims).toBe(s.body.layers.reduce((n: number, l: any) => n + l.claims, 0));
    expect(s.body.layers.every((l: any) => l.id.startsWith('public/'))).toBe(true);
    expect(s.body.last_seq).toBeGreaterThan(0);
  });

  it('JSON 404s for unknown API paths and entities; bad JSON is a 400', async () => {
    expect((await t.get('/v0/nope')).status).toBe(404);
    expect((await t.get('/v0/entities/rbx:person:nobody')).body).toEqual({ error: 'not_found', message: 'entity rbx:person:nobody not found' });
    const res = await t.app.request('/v0/query', { method: 'POST', body: '{', headers: { 'content-type': 'application/json' } });
    expect(res.status).toBe(400);
  });

  it('serves static files with SPA fallback for non-API paths', async () => {
    const asset = await t.app.request('/assets/app.js');
    expect(asset.status).toBe(200);
    expect(await asset.text()).toBe('console.log(1)');
    for (const path of ['/', '/e/rbx:person:x', '/search?q=a']) {
      const r = await t.app.request(path);
      expect(r.status, path).toBe(200);
      expect(await r.text()).toContain('<title>explorer</title>');
    }
    expect((await t.app.request('/v0/missing')).headers.get('content-type')).toContain('application/json');
  });
});
