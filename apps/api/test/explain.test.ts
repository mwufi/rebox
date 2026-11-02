/**
 * CONTRACTS §2a: under RLS only leakproof operators can use an index. Runs the API's real SQL
 * (captured from the driver) under EXPLAIN as rebox_app on a few thousand rows and checks that
 * search uses search_name's trigram index and value lookups use claim_value_text_idx.
 */
import { afterAll, beforeAll, describe, expect, it, inject } from 'vitest';
import { createApp } from '../src/app.js';
import { createDb } from '../src/db.js';
import { createVocabCache } from '../src/vocab.js';
import { createWriter } from '../src/writer.js';
import { setupApp } from './helpers.js';

const N = 4000;
const t = setupApp();
const captured: { query: string; params: unknown[] }[] = [];
const db = createDb(inject('appUrl'), { max: 2, onQuery: (query, params) => captured.push({ query, params }) });
const vocab = createVocabCache(db);
const app = createApp({
  config: { databaseUrl: '', port: 0, ingestToken: null, adminToken: null, env: 'prod' },
  db, vocab, writer: createWriter(db, vocab), log: () => {},
});
afterAll(() => db.close());

beforeAll(async () => {
  await t.owner`insert into layer (id, scope, owner, source) values ('public/bulk', 'public', null, 'bulk') on conflict do nothing`;
  await t.owner`
    insert into entity (id, kind, home_layer)
    select 'rbx:company:bulk-' || i, 'company', 'public/bulk' from generate_series(1, ${N}) i
    on conflict do nothing`;
  await t.owner`
    insert into claim (layer_id, subject, predicate, value, source_ref)
    select 'public/bulk', 'rbx:company:bulk-' || i, p.predicate, p.value, 'bulk:' || i || ':' || p.predicate
    from generate_series(1, ${N}) i,
         lateral (values ('name', to_jsonb(initcap(substr(md5(i::text), 1, 7)) || ' ' || initcap(substr(md5((i * 7)::text), 1, 5)))),
                         ('domain', to_jsonb('bulk-' || i || '.example')),
                         ('yc_batch', to_jsonb('W' || (i % 40)))) p(predicate, value)
    on conflict do nothing`;
  // Flush the GIN pending list and refresh stats, as autovacuum would on a live table.
  await t.owner`vacuum analyze claim`;
  await t.owner`vacuum analyze search_name`;
  await t.owner`vacuum analyze entity`;
});

/** EXPLAINs every statement the request ran (after rebox_set_view) as rebox_app; returns the plans. */
async function plansFor(method: string, path: string, body?: unknown): Promise<string[]> {
  captured.length = 0;
  const res = await app.request(path, {
    method,
    headers: body ? { 'content-type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  expect(res.status).toBe(200);
  const statements = [...captured];
  const plans: string[] = [];
  // Each statement runs under the view set by the rebox_set_view call that precedes it.
  let setView: { query: string; params: unknown[] } | null = null;
  for (const s of statements) {
    if (s.query.includes('rebox_set_view')) {
      setView = s;
      continue;
    }
    if (!setView || !/^\s*(with|select)/i.test(s.query)) continue;
    const view = setView;
    const rows = await db.sql.begin(async (tx) => {
      await tx.unsafe(view.query, view.params as never[]);
      return tx.unsafe(`explain ${s.query}`, s.params as never[]);
    });
    plans.push(rows.map((r) => r['QUERY PLAN']).join('\n'));
  }
  return plans;
}

describe('indexes under RLS (rebox_app)', () => {
  it('search uses search_name_trgm_idx and finds the row', async () => {
    expect(await t.owner`select count(*)::int as n from search_name where layer_id = 'public/bulk'`).toEqual([{ n: N }]);
    const target = await t.owner<{ name: string }[]>`select name from search_name where entity_id = 'rbx:company:bulk-1234'`;
    const q = target[0]!.name.split(' ')[0]!; // one word, as people type it
    const res = (await (await app.request(`/v0/search?q=${encodeURIComponent(q)}`)).json()) as { id: string }[];
    expect(res[0]!.id).toBe('rbx:company:bulk-1234');
    const plans = await plansFor('GET', `/v0/search?q=${encodeURIComponent(q)}`);
    const search = plans.find((p) => p.includes('search_name'))!;
    expect(search).toContain('search_name_trgm_idx');
    expect(search).not.toMatch(/Seq Scan on search_name/);
  });

  it('attr eq uses claim_value_text_idx', async () => {
    const plans = await plansFor('POST', '/v0/query', {
      find: 'company', where: [{ attr: { predicate: 'yc_batch', eq: 'W7' } }], limit: 5,
    });
    const plan = plans.find((p) => p.includes('cand_0'))!;
    expect(plan, plans.join('\n----\n')).toMatch(/Scan (using|on) claim_value_text_idx/);
    expect(plan).toMatch(/Index Cond: \(\(predicate = 'yc_batch'::text\) AND \(value_text = 'W7'::text\)\)/);
  });

  it('name filter in /v0/query uses search_name_trgm_idx', async () => {
    const plans = await plansFor('POST', '/v0/query', { find: 'company', where: [{ name: 'c4ca4' }], limit: 5 });
    const plan = plans.find((p) => p.includes('cand_0'))!;
    expect(plan, plans.join('\n----\n')).toMatch(/Bitmap Index Scan on search_name_trgm_idx/);
    expect(plan).not.toMatch(/Seq Scan on search_name/);
  });
});

describe('S10 plans under RLS (rebox_app)', () => {
  it('timeline "first seen" reads change through change_subject_seq_idx (0007)', async () => {
    const plans = await plansFor('GET', '/v0/entities/rbx%3Acompany%3Abulk-1234/timeline');
    const probe = plans.find((p) => p.includes('change'))!;
    expect(probe, plans.join('\n----\n')).toMatch(/change_subject_seq_idx/);
    expect(probe).not.toMatch(/Seq Scan on change/);
  });

  it('an edge-only query is driven by its candidate set, not a hash join over every entity', async () => {
    const plans = await plansFor('POST', '/v0/query', { find: 'company', where: [{ attr: { predicate: 'yc_batch', gte: 'W30' } }], limit: 5 });
    const plan = plans.find((p) => p.includes('cand_0'))!;
    expect(plan, plans.join('\n----\n')).toMatch(/CTE cand_0/);
    expect(plan).toMatch(/Index Scan using entity_pkey on entity/);
    expect(plan).not.toMatch(/Seq Scan on entity/);
  });
});

describe('S13 hub plans under RLS (rebox_app)', () => {
  const HUB = 'rbx:place:bulk-hub';
  const DEGREE = 1500;
  beforeAll(async () => {
    // A layer the anonymous viewer cannot see, so entity visibility must be probed per key (the
    // all-layers-visible shortcut does not apply).
    await t.owner`insert into layer (id, scope, owner, source) values ('user/s13x/gmail', 'user', 's13x', 'gmail') on conflict do nothing`;
    await t.owner`insert into entity (id, kind, home_layer) values (${HUB}, 'place', 'public/bulk') on conflict do nothing`;
    await t.owner`
      insert into claim (layer_id, subject, predicate, object_id, source_ref)
      select 'public/bulk', 'rbx:company:bulk-' || i, 'based_in', ${HUB}, 'bulk:' || i || ':based_in'
      from generate_series(1, ${DEGREE}) i
      on conflict do nothing`;
    await t.owner`vacuum analyze claim`;
  });

  /**
   * EXPLAIN ANALYZE of the generic plan (what the API runs: views and edges set plan_cache_mode =
   * force_generic_plan) for each statement of a request that mentions `marker`.
   */
  async function genericPlans(path: string, marker: string): Promise<string[]> {
    captured.length = 0;
    const res = await app.request(path);
    expect(res.status).toBe(200);
    const statements = [...captured];
    const lit = (v: unknown) =>
      Array.isArray(v) ? `'{${v.map((x) => JSON.stringify(String(x))).join(',')}}'` : `'${String(v).replace(/'/g, "''")}'`;
    const plans: string[] = [];
    let setView: { query: string; params: unknown[] } | null = null;
    for (const s of statements) {
      if (s.query.includes('rebox_set_view')) setView = s;
      if (!setView || !s.query.includes(marker)) continue;
      const view = setView;
      const rows = await db.sql.begin(async (tx) => {
        await tx.unsafe(view.query, view.params as never[]);
        await tx.unsafe(`set local plan_cache_mode = force_generic_plan`);
        await tx.unsafe(`prepare s13_plan as ${s.query}`);
        try {
          return await tx.unsafe(`explain (analyze, costs off) execute s13_plan(${s.params.map(lit).join(', ')})`);
        } finally {
          await tx.unsafe(`deallocate s13_plan`);
        }
      });
      plans.push(rows.map((r) => r['QUERY PLAN']).join('\n'));
    }
    return plans;
  }

  /** Total rows the fold read from claim by primary key (the claims behind the picked keys). */
  const pkeyReads = (plan: string) =>
    [...plan.matchAll(/Index Scan using claim_pkey on claim c .*?rows=(\d+) loops=(\d+)/g)].reduce(
      (n, m) => n + Number(m[1]) * Number(m[2]),
      0,
    );

  it('an edges page folds only the page, not the hub', async () => {
    const plans = await genericPlans(`/v0/entities/${encodeURIComponent(HUB)}/edges?dir=in&limit=5`, 'picked');
    expect(plans).toHaveLength(1);
    const plan = plans[0]!;
    expect(plan).toMatch(/(Index|Bitmap Index) Scan (using|on) claim_object_idx/); // one pass over the hub's keys
    expect(plan).not.toMatch(/Seq Scan on entity/); // visibility is probed per key, never by reading every entity
    expect(pkeyReads(plan)).toBe(6); // limit + 1 keys, one claim each
    // Visibility is probed key by key and stops with the page.
    const probes = [...plan.matchAll(/Index Scan using entity_pkey on entity e_\d+ .*?loops=(\d+)/g)].map((m) => Number(m[1]));
    expect(probes.length).toBeGreaterThan(0);
    for (const loops of probes) expect(loops).toBeLessThanOrEqual(6);
  });

  it('the entity view folds at most 12 keys per predicate and counts the rest', async () => {
    const plans = await genericPlans(`/v0/entities/${encodeURIComponent(HUB)}`, 'numbered');
    const incoming = plans.find((p) => /claim_object_idx/.test(p))!;
    expect(incoming, plans.join('\n----\n')).toBeTruthy();
    expect(pkeyReads(incoming)).toBe(12);
    expect(incoming).not.toMatch(/Seq Scan on entity/);
    const body = (await (await app.request(`/v0/entities/${encodeURIComponent(HUB)}`)).json()) as {
      incoming: unknown[];
      incoming_counts: Record<string, number>;
    };
    expect(body.incoming).toHaveLength(12);
    expect(body.incoming_counts).toEqual({ based_in: DEGREE });
  });
});
