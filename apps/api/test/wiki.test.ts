/**
 * GET/POST /v0/entities/:id/wiki (CONTRACTS §14) with a fake model: prompt input holds only
 * visible public facts, caching + staleness, auth, rate limit, footnote post-processing.
 */
import { afterAll, beforeAll, describe, expect, it, inject } from 'vitest';
import { createApp } from '../src/app.js';
import { createDb } from '../src/db.js';
import { createVocabCache } from '../src/vocab.js';
import type { WikiCompletion, WikiModel, WikiPrompt } from '../src/wiki/model.js';
import { finishArticle, sourceUrl } from '../src/wiki/input.js';
import { createLimiter, RateLimited } from '../src/wiki/service.js';
import { createWriter } from '../src/writer.js';
import { ADMIN, INGEST, setupApp } from './helpers.js';

const t = setupApp({ env: 'dev' });
const P = 'public/s10wk';
const PRIV = 'user/alice/s10wk';
const co = 'rbx:company:s10wk-acme';
const founder = 'rbx:person:s10wk-maya';
const hidden = 'rbx:person:s10wk-hidden';

/** Records every prompt; answers with a canned article citing sources 1, 2 and a bogus 99. */
function fakeModel(): WikiModel & { prompts: WikiPrompt[]; fail: boolean } {
  const m = {
    id: 'fake-model',
    prompts: [] as WikiPrompt[],
    fail: false,
    async complete(p: WikiPrompt): Promise<WikiCompletion> {
      m.prompts.push(p);
      if (m.fail) throw new Error('upstream down');
      return {
        text: 'Acme Wk is a company.[^1] It was founded by Maya Wk.[^2] Its revenue is unknown.[^99]\n\n## References\n[^1]: made up',
        model: 'fake-model-2026',
        input_tokens: 100,
        output_tokens: 50,
        stop_reason: 'end_turn',
      };
    },
  };
  return m;
}

function wikiApp(opts: { public?: boolean; max?: number; model?: WikiModel | null } = {}) {
  const db = createDb(inject('appUrl'), { max: 3 });
  const vocab = createVocabCache(db);
  const model = opts.model === undefined ? fakeModel() : opts.model;
  const app = createApp({
    config: { databaseUrl: '', port: 0, ingestToken: INGEST, adminToken: ADMIN, env: 'dev' },
    db,
    vocab,
    writer: createWriter(db, vocab),
    log: () => {},
    wiki: { model, public: opts.public ?? false, rateLimit: { max: opts.max ?? 100, windowMs: 60_000 } },
  });
  const call = async (method: string, path: string, headers: Record<string, string> = {}) => {
    const res = await app.request(path, { method, headers });
    return { status: res.status, body: (await res.json()) as any, headers: res.headers };
  };
  return { app, db, model: model as ReturnType<typeof fakeModel>, call };
}

const apps: { db: { close(): Promise<void> } }[] = [];
const make = (o: Parameters<typeof wikiApp>[0] = {}) => {
  const a = wikiApp(o);
  apps.push(a);
  return a;
};
afterAll(async () => {
  for (const a of apps) await a.db.close();
});

const admin = { authorization: `Bearer ${ADMIN}` };
const path = (id: string, qs = '') => `/v0/entities/${encodeURIComponent(id)}/wiki${qs}`;

async function claim(layer: string, subject: string, predicate: string, opts: { value?: unknown; object?: string; from?: string; ref?: string } = {}) {
  await t.owner`
    insert into claim (layer_id, subject, predicate, object_id, value, valid_from, source_ref)
    values (${layer}, ${subject}, ${predicate}, ${opts.object ?? null},
            ${opts.value === undefined ? null : t.owner.json(opts.value as never)}, ${opts.from ?? null},
            ${opts.ref ?? `s10wk:${subject}:${predicate}`})`;
}

beforeAll(async () => {
  await t.owner`insert into layer (id, scope, owner, source) values (${P}, 'public', null, 's10wk'), (${PRIV}, 'user', 'alice', 's10wk') on conflict do nothing`;
  await t.owner`insert into entity (id, kind, home_layer) values (${co}, 'company', ${P}), (${founder}, 'person', ${P}), (${hidden}, 'person', ${PRIV}) on conflict do nothing`;
  await claim(P, co, 'name', { value: 'Acme Wk', ref: 'wikidata:Q999001:name' });
  await claim(P, co, 'description', { value: 'maker of public widgets', ref: 'wikidata:Q999001:description' });
  await claim(P, founder, 'name', { value: 'Maya Wk', ref: 'hn:user:mayawk:name' });
  await claim(P, founder, 'founded', { object: co, ref: 'hn:story:4242:founded:company' });
  await claim(P, co, 'milestone', { value: { title: 'Public launch day', kind: 'launch' }, from: '2020-02-02', ref: 'x:acmewk:milestone:company' });
  // Private facts that must never reach the model, even for a viewer who can see them.
  await claim(PRIV, co, 'description', { value: 'PRIVATE pivot plans', ref: 'priv:desc' });
  await claim(PRIV, co, 'milestone', { value: { title: 'PRIVATE term sheet', kind: 'funding' }, from: '2021-01-01', ref: 'priv:ms' });
  await claim(PRIV, hidden, 'name', { value: 'PRIVATE Person', ref: 'priv:name' });
  await claim(PRIV, hidden, 'founded', { object: co, ref: 'priv:founded' });
});

describe('wiki generation', () => {
  it('GET is 404 before generation; POST generates, caches, and GET returns it', async () => {
    const a = make();
    const before = await a.call('GET', path(co));
    expect(before.status).toBe(404);
    expect(before.body).toMatchObject({ error: 'not_found', can_generate: true, public: false });
    expect((await a.call('GET', path('rbx:company:s10wk-nope'))).status).toBe(404);

    const gen = await a.call('POST', path(co), admin);
    expect(gen.status, JSON.stringify(gen.body)).toBe(200);
    expect(gen.body).toMatchObject({ model: 'fake-model-2026', stale: false, entity_id: co });
    expect(gen.body.input_seq).toBeGreaterThan(0);
    expect(typeof gen.body.generated_at).toBe('string');
    // Footnotes: known sources become definitions with real URLs, the bogus [^99] and the
    // model's own references section are dropped.
    expect(gen.body.content_md).toContain('[^1]: [Wikidata Q999001](https://www.wikidata.org/wiki/Q999001)');
    const two = gen.body.sources.find((s: any) => s.n === 2);
    expect(gen.body.content_md).toContain(`[^2]: [${two.label}](${two.url})`);
    // Every source the prompt offered is a real URL derived from the fact's source_ref.
    expect(a.model.prompts[0]!.user).toContain('[source 2]');
    expect(a.model.prompts[0]!.user).toContain('https://news.ycombinator.com/item?id=4242');
    expect(gen.body.content_md).not.toContain('[^99]');
    expect(gen.body.content_md).not.toContain('made up');
    expect(gen.body.content_md).not.toContain('## References');
    expect(gen.body.sources.map((s: any) => s.n)).toEqual([1, 2]);

    const got = await a.call('GET', path(co));
    expect(got.status).toBe(200);
    expect(got.body.content_md).toBe(gen.body.content_md);
    expect(got.body.stale).toBe(false);

    // Fresh article: a second POST does not call the model again.
    const calls = a.model.prompts.length;
    expect((await a.call('POST', path(co), admin)).status).toBe(200);
    expect(a.model.prompts.length).toBe(calls);
  });

  it('prompt input holds only visible public facts, even when a private viewer asks', async () => {
    const a = make({ public: true });
    const r = await a.call('POST', path(co, '?force=true'), { 'x-rebox-viewer': 'alice' });
    expect(r.status, JSON.stringify(r.body)).toBe(200);
    const p = a.model.prompts.at(-1)!;
    expect(p.user).toContain('maker of public widgets');
    expect(p.user).toContain('Public launch day');
    expect(p.user).toContain('Maya Wk');
    expect(p.user).toContain('https://www.wikidata.org/wiki/Q999001');
    expect(p.user).not.toContain('PRIVATE');
    expect(p.user).not.toContain(hidden);
    expect(p.user).not.toContain(PRIV);
    expect(p.system).toContain("Rebox doesn't know yet");
    expect(JSON.stringify(r.body)).not.toContain('PRIVATE');
  });

  it('becomes stale when a newer public change touches the entity; POST regenerates', async () => {
    const a = make();
    await a.call('POST', path(co, '?force=true'), admin);
    expect((await a.call('GET', path(co))).body.stale).toBe(false);
    // A private change does not make the public article stale.
    await claim(PRIV, co, 'headcount', { value: 7, ref: 'priv:hc' });
    expect((await a.call('GET', path(co))).body.stale).toBe(false);
    // A public change on an incoming edge does.
    await claim(P, founder, 'worked_at', { object: co, ref: 's10wk:maya:worked' });
    const stale = await a.call('GET', path(co));
    expect(stale.body.stale).toBe(true);
    const n = a.model.prompts.length;
    const regen = await a.call('POST', path(co), admin);
    expect(regen.body.stale).toBe(false);
    expect(regen.body.input_seq).toBeGreaterThan(stale.body.input_seq);
    expect(a.model.prompts.length).toBe(n + 1);
  });

  it('POST needs the admin token unless WIKI_PUBLIC; 503 without a model', async () => {
    const a = make();
    expect((await a.call('POST', path(co))).status).toBe(401);
    expect((await a.call('POST', path(co), { authorization: `Bearer ${INGEST}` })).status).toBe(403);
    expect((await a.call('POST', path(co), { authorization: 'Bearer wrong' })).status).toBe(401);
    expect((await a.call('POST', path('rbx:company:s10wk-nope'), admin)).status).toBe(404);
    const pub = make({ public: true });
    expect((await pub.call('POST', path(co))).status).toBe(200);
    const off = make({ model: null });
    const r = await off.call('POST', path(co), admin);
    expect(r.status).toBe(503);
    // Reading cached articles still works without a model.
    expect((await off.call('GET', path(co))).status).toBe(200);
  });

  it('rate-limits generation (429 + Retry-After) and maps model failures to 502', async () => {
    const a = make({ max: 2 });
    const force = path(co, '?force=true');
    expect((await a.call('POST', force, admin)).status).toBe(200);
    expect((await a.call('POST', force, admin)).status).toBe(200);
    const limited = await a.call('POST', force, admin);
    expect(limited.status).toBe(429);
    expect(limited.body.error).toBe('rate_limited');
    expect(Number(limited.headers.get('retry-after'))).toBeGreaterThan(0);

    const b = make();
    b.model.fail = true;
    const failed = await b.call('POST', force, admin);
    expect(failed.status).toBe(502);
    expect(JSON.stringify(failed.body)).not.toContain('upstream down');
  });
});

describe('wiki helpers', () => {
  it('sliding-window limiter', () => {
    let now = 0;
    const l = createLimiter(2, 1000, () => now);
    l.acquire();
    l.acquire();
    expect(() => l.acquire()).toThrow(RateLimited);
    now = 1001;
    expect(() => l.acquire()).not.toThrow();
  });

  it('source URLs from evidence first, then source_ref', () => {
    expect(sourceUrl({ source: 'x', source_ref: 'x:jack:name:person', evidence: [{ url: 'https://x.com/jack/status/1', author: 'jack' }] }))
      .toEqual({ url: 'https://x.com/jack/status/1', label: 'jack (x.com)' });
    expect(sourceUrl({ source: 'hn', source_ref: 'hn:user:pg:name' })?.url).toBe('https://news.ycombinator.com/user?id=pg');
    expect(sourceUrl({ source: 'github', source_ref: 'github:org:stripe:name' })?.url).toBe('https://github.com/stripe');
    expect(sourceUrl({ source: 'manual', source_ref: 'seed:1' })).toBeNull();
  });

  it('finishArticle unwraps fences and renumbers nothing it does not know', () => {
    const out = finishArticle('```markdown\nHi.[^2] Bye.[^3]\n```', [
      { n: 1, url: 'https://a.example', label: 'A' },
      { n: 2, url: 'https://b.example', label: 'B' },
    ]);
    expect(out.content_md).toBe('Hi.[^2] Bye.\n\n[^2]: [B](https://b.example)\n');
    expect(out.cited.map((s) => s.n)).toEqual([2]);
  });
});
