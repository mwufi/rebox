import { describe, expect, it } from 'vitest';
import { ApiError, createClient, isRedirect } from '../src/api';
import { ODILE, mockFetch } from './fixtures';

describe('api client', () => {
  it('builds GET URLs with encoded params and skips empty ones', async () => {
    const m = mockFetch({ 'GET /v0/search*': () => [], 'GET /v0/entities*': () => [], 'GET /v0/changes*': () => ({ changes: [], next: 0 }) });
    const c = createClient({ base: 'http://h.test/', fetch: m.fetch });
    await c.search('maya chen & co', { kind: 'person', limit: 5 });
    await c.search('x', {});
    await c.edges('rbx:company:a b', { dir: 'in', predicate: 'worked_at', cursor: null });
    await c.entities(['rbx:person:a', 'rbx:person:b']);
    await c.changes({ after: 10, limit: 50 });
    expect(m.calls.map((x) => x.url)).toEqual([
      'http://h.test/v0/search?q=maya%20chen%20%26%20co&kind=person&limit=5',
      'http://h.test/v0/search?q=x',
      'http://h.test/v0/entities/rbx%3Acompany%3Aa%20b/edges?predicate=worked_at&dir=in',
      'http://h.test/v0/entities?ids=rbx%3Aperson%3Aa%2Crbx%3Aperson%3Ab',
      'http://h.test/v0/changes?after=10&limit=50',
    ]);
  });

  it('does not call the API for an empty id list', async () => {
    const m = mockFetch({});
    expect(await createClient({ fetch: m.fetch }).entities([])).toEqual([]);
    expect(m.calls).toHaveLength(0);
  });

  it('POSTs JSON for queries', async () => {
    const m = mockFetch({ 'POST /v0/query': (b) => ({ results: [], next: null, echo: b }) });
    const c = createClient({ fetch: m.fetch });
    await c.query({ find: 'person', where: [{ name: 'chen' }], limit: 10 });
    expect(m.calls[0]).toMatchObject({ method: 'POST', url: '/v0/query', body: { find: 'person', where: [{ name: 'chen' }], limit: 10 } });
    expect(m.calls[0]!.headers['content-type']).toBe('application/json');
    expect(m.calls[0]!.headers.authorization).toBeUndefined();
  });

  it('sends the admin token only on admin calls', async () => {
    const m = mockFetch({
      'GET /v0/review*': () => [],
      'POST /v0/admin/merge': () => ({ merged: {}, review_id: 3, seq: 9 }),
      'POST /v0/admin/split': () => ({ split: {}, seq: 10 }),
      'POST /v0/admin/review/3/dismiss': () => ({ id: 3 }),
    });
    const c = createClient({ fetch: m.fetch });
    await c.review('sekret');
    await c.merge('sekret', { from: 'rbx:company:b', into: 'rbx:company:a', review_id: 3 });
    await c.split('sekret', 'rbx:company:b');
    await c.dismiss('sekret', 3);
    expect(m.calls.map((x) => `${x.method} ${x.url}`)).toEqual([
      'GET /v0/review?status=open',
      'POST /v0/admin/merge',
      'POST /v0/admin/split',
      'POST /v0/admin/review/3/dismiss',
    ]);
    expect(m.calls.every((x) => x.headers.authorization === 'Bearer sekret')).toBe(true);
    expect(m.calls[1]!.body).toEqual({ from: 'rbx:company:b', into: 'rbx:company:a', review_id: 3 });
    expect(m.calls[2]!.body).toEqual({ id: 'rbx:company:b' });
  });

  it('throws ApiError with the contract error body', async () => {
    const m = mockFetch({
      'POST /v0/query': () => new Response(JSON.stringify({ error: 'validation', details: [{ index: 0, message: 'unknown predicate "x"' }] }), { status: 400 }),
      'GET /v0/entities/rbx%3Aperson%3Anope': () => new Response(JSON.stringify({ error: 'not_found', message: 'entity rbx:person:nope not found' }), { status: 404 }),
    });
    const c = createClient({ fetch: m.fetch });
    const e1 = await c.query({ find: 'person', where: [] }).catch((e) => e);
    expect(e1).toBeInstanceOf(ApiError);
    expect(e1.status).toBe(400);
    expect(e1.message).toBe('unknown predicate "x"');
    const e2 = await c.entity('rbx:person:nope').catch((e) => e);
    expect(e2.status).toBe(404);
    expect(e2.message).toBe('entity rbx:person:nope not found');
  });

  it('distinguishes redirects from entity views', async () => {
    const m = mockFetch({
      'GET /v0/entities/rbx%3Aperson%3Aodile': () => ODILE,
      'GET /v0/entities/rbx%3Aperson%3Aodile-hn': () => ({ redirect: ODILE.id, entity: ODILE }),
    });
    const c = createClient({ fetch: m.fetch });
    expect(isRedirect(await c.entity('rbx:person:odile'))).toBe(false);
    const r = await c.entity('rbx:person:odile-hn');
    expect(isRedirect(r) && r.redirect).toBe(ODILE.id);
  });
});
