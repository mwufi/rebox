/**
 * Read-latency benchmark (S10, S13 hub cases): runs the API in-process against DATABASE_URL (role rebox_app, RLS
 * applies) and prints p50/p95 per endpoint. Usage:
 *   DATABASE_URL=postgres://rebox_app:...@host/db pnpm --filter @rebox/api bench [-- --n 60]
 */
import { createApp } from '../src/app.js';
import { createDb } from '../src/db.js';
import { createVocabCache } from '../src/vocab.js';
import { createWriter } from '../src/writer.js';

const url = process.env.DATABASE_URL;
if (!url) throw new Error('DATABASE_URL is required');
const nArg = process.argv.indexOf('--n');
const N = nArg > 0 ? Number(process.argv[nArg + 1]) : 60;
const only = (() => {
  const i = process.argv.indexOf('--only');
  return i > 0 ? process.argv[i + 1] : undefined;
})();

const db = createDb(url, { max: 4 });
const vocab = createVocabCache(db);
const app = createApp({
  config: { databaseUrl: url, port: 0, ingestToken: null, adminToken: null, env: 'prod' },
  db,
  vocab,
  writer: createWriter(db, vocab),
  log: () => {},
});

async function idOf(name: string, kind: string): Promise<string> {
  const res = await app.request(`/v0/search?q=${encodeURIComponent(name)}&kind=${kind}&limit=1`);
  const hits = (await res.json()) as { id: string }[];
  if (!hits[0]) throw new Error(`no ${kind} named ${name}`);
  return hits[0].id;
}

type Case = { name: string; method: 'GET' | 'POST'; path: string; body?: unknown };

const q = (find: string, where: unknown[]) => ({ find, where, limit: 50 });

async function cases(): Promise<Case[]> {
  const brex = await idOf('Brex', 'company');
  const patrick = await idOf('Patrick Collison', 'person');
  const sf = await idOf('San Francisco', 'place');
  // S13: davidbooth is an outgoing hub (X follows); a person with a handful of facts is the control.
  const booth = await idOf('David Booth', 'person');
  const person = await idOf(process.env.BENCH_PERSON ?? 'Jessica Lessin', 'person');
  const enc = encodeURIComponent;
  return [
    { name: 'search "stripe"', method: 'GET', path: '/v0/search?q=stripe' },
    { name: 'search "pat"', method: 'GET', path: '/v0/search?q=pat' },
    { name: 'entity Brex', method: 'GET', path: `/v0/entities/${enc(brex)}` },
    { name: 'entity Patrick Collison', method: 'GET', path: `/v0/entities/${enc(patrick)}` },
    { name: 'entity San Francisco (hub)', method: 'GET', path: `/v0/entities/${enc(sf)}` },
    { name: 'edges SF in (50)', method: 'GET', path: `/v0/entities/${enc(sf)}/edges?dir=in&limit=50` },
    { name: 'edges SF in based_in (50)', method: 'GET', path: `/v0/entities/${enc(sf)}/edges?dir=in&predicate=based_in&limit=50` },
    { name: 'entity davidbooth (out hub)', method: 'GET', path: `/v0/entities/${enc(booth)}` },
    { name: 'edges davidbooth follows (50)', method: 'GET', path: `/v0/entities/${enc(booth)}/edges?dir=out&predicate=follows&limit=50` },
    { name: 'entity random person', method: 'GET', path: `/v0/entities/${enc(person)}` },
    { name: 'timeline Brex', method: 'GET', path: `/v0/entities/${enc(brex)}/timeline` },
    { name: 'timeline Patrick Collison', method: 'GET', path: `/v0/entities/${enc(patrick)}/timeline` },
    { name: 'q: founded a company', method: 'POST', path: '/v0/query', body: q('person', [{ edge: { predicate: 'founded', dir: 'out' } }]) },
    { name: 'q: founded_year >= 2020', method: 'POST', path: '/v0/query', body: q('company', [{ attr: { predicate: 'founded_year', gte: 2020 } }]) },
    {
      name: 'q: worked_at during 2018-2021',
      method: 'POST',
      path: '/v0/query',
      body: q('person', [{ edge: { predicate: 'worked_at', dir: 'out', during: { from: '2018-01-01', to: '2021-12-31' } } }]),
    },
    { name: 'q: shipped a launch', method: 'POST', path: '/v0/query', body: q('company', [{ edge: { predicate: 'launched', dir: 'out' } }]) },
    { name: 'q: yc_batch = W17', method: 'POST', path: '/v0/query', body: q('company', [{ attr: { predicate: 'yc_batch', eq: 'W17' } }]) },
  ];
}

const pct = (sorted: number[], p: number) => sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)]!;

async function run(c: Case): Promise<{ p50: number; p95: number; status: number }> {
  const once = async () => {
    const t = performance.now();
    const res = await app.request(c.path, {
      method: c.method,
      headers: c.body ? { 'content-type': 'application/json' } : {},
      body: c.body ? JSON.stringify(c.body) : undefined,
    });
    await res.arrayBuffer();
    return { ms: performance.now() - t, status: res.status };
  };
  for (let i = 0; i < 5; i++) await once();
  const times: number[] = [];
  let status = 0;
  for (let i = 0; i < N; i++) {
    const r = await once();
    times.push(r.ms);
    status = r.status;
  }
  times.sort((a, b) => a - b);
  return { p50: pct(times, 50), p95: pct(times, 95), status };
}

const all = await cases();
console.log(`| case | status | p50 ms | p95 ms |\n| --- | --- | --- | --- |`);
for (const c of all) {
  if (only && !c.name.includes(only)) continue;
  const r = await run(c);
  console.log(`| ${c.name} | ${r.status} | ${r.p50.toFixed(1)} | ${r.p95.toFixed(1)} |`);
}
await db.close();
