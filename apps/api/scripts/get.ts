/** Dev helper: one in-process GET against DATABASE_URL, prints status + body.  tsx scripts/get.ts <path> */
import { createApp } from '../src/app.js';
import { createDb } from '../src/db.js';
import { createVocabCache } from '../src/vocab.js';
import { createWriter } from '../src/writer.js';

const url = process.env.DATABASE_URL;
if (!url) throw new Error('DATABASE_URL is required');
const db = createDb(url, { max: 2 });
const vocab = createVocabCache(db);
const app = createApp({
  config: { databaseUrl: url, port: 0, ingestToken: null, adminToken: null, env: 'prod' },
  db, vocab, writer: createWriter(db, vocab), log: () => {},
});
const res = await app.request(process.argv[2] ?? '/health');
console.log(res.status, await res.text());
await db.close();
