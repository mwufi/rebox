/** Process entry: `node dist/server.js` (CONTRACTS §11). */
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { serve } from '@hono/node-server';
import { createApp } from './app.js';
import { loadConfig } from './config.js';
import { createDb } from './db.js';
import { createVocabCache } from './vocab.js';
import { createWriter } from './writer.js';

const config = loadConfig();
const db = createDb(config.databaseUrl);
const vocab = createVocabCache(db);
const writer = createWriter(db, vocab);
// public/ sits next to dist/ (and src/), independent of the working directory.
const publicDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'public');

const app = createApp({ config, db, vocab, writer, publicDir });
const server = serve({ fetch: app.fetch, port: config.port }, (info) => {
  console.log(JSON.stringify({ at: new Date().toISOString(), level: 'info', msg: 'listening', port: info.port, env: config.env }));
});

let stopping = false;
function shutdown(signal: string) {
  if (stopping) return;
  stopping = true;
  console.log(JSON.stringify({ at: new Date().toISOString(), level: 'info', msg: 'shutting down', signal }));
  const force = setTimeout(() => process.exit(1), 10_000);
  force.unref();
  server.close(() => {
    db.close().finally(() => process.exit(0));
  });
}
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
