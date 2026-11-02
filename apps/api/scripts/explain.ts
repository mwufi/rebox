/**
 * EXPLAIN ANALYZE every statement one API request runs, as rebox_app under the same view. When the
 * request asked for generic plans (S13: entity views and edge pages set plan_cache_mode), each
 * statement is prepared and its generic plan explained, since that is the plan the API runs.
 *   DATABASE_URL=... tsx scripts/explain.ts GET '/v0/search?q=x'
 *   DATABASE_URL=... tsx scripts/explain.ts POST /v0/query '{"find":"person","where":[...]}'
 */
import { createApp } from '../src/app.js';
import { createDb } from '../src/db.js';
import { createVocabCache } from '../src/vocab.js';
import { createWriter } from '../src/writer.js';

const url = process.env.DATABASE_URL;
if (!url) throw new Error('DATABASE_URL is required');
const [method = 'GET', path = '/health', body] = process.argv.slice(2);
const captured: { query: string; params: unknown[] }[] = [];
const db = createDb(url, { max: 2, onQuery: (query, params) => captured.push({ query, params }) });
const vocab = createVocabCache(db);
const app = createApp({
  config: { databaseUrl: url, port: 0, ingestToken: null, adminToken: null, env: 'prod' },
  db, vocab, writer: createWriter(db, vocab), log: () => {},
});
const send = () =>
  app.request(path, { method, headers: body ? { 'content-type': 'application/json' } : {}, body: body ?? undefined });
await (await send()).arrayBuffer(); // warm caches
captured.length = 0;
const res = await send();
console.log(`status ${res.status}`);
const lit = (v: unknown) => (Array.isArray(v) ? `'{${v.map((x) => JSON.stringify(String(x))).join(',')}}'` : `'${String(v).replace(/'/g, "''")}'`);
const opts = `analyze, buffers${process.env.COSTS ? '' : ', costs off'}`;
let setView: { query: string; params: unknown[] } | null = null;
let generic = false;
for (const s of [...captured]) {
  if (s.query.includes('rebox_set_view')) {
    setView = s;
    generic = false;
    continue;
  }
  if (s.query.includes('plan_cache_mode')) {
    generic = true;
    continue;
  }
  if (!setView || !/^\s*(with|select)/i.test(s.query)) continue;
  const view = setView;
  const asGeneric = generic;
  const rows = await db.sql.begin(async (tx) => {
    await tx.unsafe(view.query, view.params as never[]);
    if (!asGeneric) return tx.unsafe(`explain (${opts}) ${s.query}`, s.params as never[]);
    await tx.unsafe(`set local plan_cache_mode = force_generic_plan`);
    await tx.unsafe(`prepare explain_stmt as ${s.query}`);
    try {
      return await tx.unsafe(`explain (${opts}) execute explain_stmt(${s.params.map(lit).join(', ')})`);
    } finally {
      await tx.unsafe(`deallocate explain_stmt`);
    }
  });
  console.log(`\n=== ${asGeneric ? '[generic] ' : ''}` + s.query.replace(/\s+/g, ' ').slice(0, 140));
  if (process.env.SHOW_SQL) console.log(s.query.replace(/\$(\d+)/g, (_m, i) => lit(s.params[Number(i) - 1])) + ';');
  console.log(rows.map((r) => r['QUERY PLAN']).join('\n'));
}
await db.close();
