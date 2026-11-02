import postgres from 'postgres';
import { afterAll, inject } from 'vitest';
import { createApp } from '../src/app.js';
import type { Config } from '../src/config.js';
import { createDb } from '../src/db.js';
import { createVocabCache } from '../src/vocab.js';
import { createWriter } from '../src/writer.js';

export const INGEST = 'test-ingest-token';
export const ADMIN = 'test-admin-token';
/** The internal worker every helper write is attributed to (CONTRACTS §12). */
export const TEST_WORKER = 'test-connector-000001';

/** In-process app on the APP url (rebox_app; RLS applies), plus an owner connection for setup. */
export function setupApp(opts: { env?: 'dev' | 'prod'; publicDir?: string } = {}) {
  const appUrl = inject('appUrl');
  const ownerUrl = inject('ownerUrl');
  const config: Config = { databaseUrl: appUrl, port: 0, ingestToken: INGEST, adminToken: ADMIN, env: opts.env ?? 'dev' };
  const db = createDb(appUrl, { max: 4 });
  const vocab = createVocabCache(db);
  const writer = createWriter(db, vocab);
  const logs: Record<string, unknown>[] = [];
  const app = createApp({ config, db, vocab, writer, publicDir: opts.publicDir ?? null, log: (l) => logs.push(l) });
  const owner = postgres(ownerUrl, { max: 2, onnotice: () => {}, connection: { TimeZone: 'UTC' } });
  afterAll(async () => {
    await db.close();
    await owner.end({ timeout: 5 });
  });

  async function req(method: string, path: string, init: { body?: unknown; headers?: Record<string, string> } = {}) {
    const headers: Record<string, string> = { ...init.headers };
    if (init.body !== undefined) headers['content-type'] = 'application/json';
    const res = await app.request(path, {
      method,
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
    const text = await res.text();
    let json: any = null;
    try {
      json = JSON.parse(text);
    } catch {
      json = text;
    }
    return { status: res.status, body: json, headers: res.headers };
  }

  const ingest = { authorization: `Bearer ${INGEST}` };
  /** Registers (or extends) an internal worker so it may write `layer`; returns its id. */
  async function worker(layer: string, opts: { name?: string; kind?: string; id?: string } = {}): Promise<string> {
    const name = opts.name ?? 'test';
    const kind = opts.kind ?? 'connector';
    const body: Record<string, unknown> = { name, kind, source: name, layers: [layer] };
    if (opts.id ?? (name === 'test' && kind === 'connector')) body.id = opts.id ?? TEST_WORKER;
    const r = await req('POST', '/internal/workers', { body, headers: ingest });
    if (r.status !== 200 && r.status !== 201) throw new Error(`register worker: ${r.status} ${JSON.stringify(r.body)}`);
    return r.body.id as string;
  }
  const registered = new Set<string>();

  return {
    app,
    db,
    owner,
    logs,
    worker,
    get: (path: string, headers?: Record<string, string>) => req('GET', path, { headers }),
    post: (path: string, body: unknown, headers?: Record<string, string>) => req('POST', path, { body, headers }),
    patch: (path: string, body: unknown, headers?: Record<string, string>) => req('PATCH', path, { body, headers }),
    /** POST /internal/claims with the ingest token, as TEST_WORKER (registered for the layer) or `opts.worker`. */
    write: async (layer: string, claims: unknown[], opts: { worker?: string; run_id?: number } = {}) => {
      if (!opts.worker && !registered.has(layer)) {
        await worker(layer);
        registered.add(layer);
      }
      const body: Record<string, unknown> = { layer, worker: opts.worker ?? TEST_WORKER, claims };
      if (opts.run_id !== undefined) body.run_id = opts.run_id;
      return req('POST', '/internal/claims', { body, headers: ingest });
    },
  };
}

let counter = 0;
/** A unique, valid entity id per call (tests share one database). */
export function uid(kind: string, hint = 'x'): string {
  counter += 1;
  return `rbx:${kind}:${hint}-${process.pid}-${Date.now().toString(36)}-${counter}`;
}

/** Minimal claim builders. */
export const mint = (id: string) => ({ mint: { id, kind: id.split(':')[1]! } });
export const ref = (id: string) => ({ id });
export const nameClaim = (id: string, name: string, src = 'test', minted = true) => ({
  subject: minted ? mint(id) : ref(id),
  predicate: 'name',
  value: name,
  source_ref: `${src}:${id}:name`,
});
