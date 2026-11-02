/** The Hono app: middleware (request id, JSON logs, auth, CORS), routes, static explorer. */
import { existsSync, statSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { serveStatic } from '@hono/node-server/serve-static';
import { Hono } from 'hono';
import { bodyLimit } from 'hono/body-limit';
import { cors } from 'hono/cors';
import { requestId, type RequestIdVariables } from 'hono/request-id';
import { authenticate, type Principal } from './auth.js';
import type { Config } from './config.js';
import type { Db } from './db.js';
import { ApiError, notFound } from './errors.js';
import { adminRoutes } from './routes/admin.js';
import { internalRoutes } from './routes/internal.js';
import { readRoutes } from './routes/read.js';
import { timelineRoutes } from './routes/timeline.js';
import { wikiRoutes } from './routes/wiki.js';
import { workerRoutes } from './routes/workers.js';
import type { VocabCache } from './vocab.js';
import type { WikiSettings } from './wiki/model.js';
import type { Writer } from './writer.js';

export interface Deps {
  config: Config;
  db: Db;
  vocab: VocabCache;
  writer: Writer;
  /** Static explorer build; served only if the directory exists. */
  publicDir?: string | null;
  log?: (line: Record<string, unknown>) => void;
  /** Wiki generation (S10); absent = disabled. */
  wiki?: WikiSettings;
}

export type AppEnv = { Variables: RequestIdVariables & { principal: Principal } };

const API_PREFIXES = ['/v0', '/internal', '/health'];
const isApiPath = (path: string) => API_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`));

export function createApp(deps: Deps) {
  const log = deps.log ?? ((line) => process.stdout.write(`${JSON.stringify(line)}\n`));
  const app = new Hono<AppEnv>();

  app.use(requestId());
  app.use(async (c, next) => {
    const start = performance.now();
    await next();
    log({
      at: new Date().toISOString(),
      level: c.res.status >= 500 ? 'error' : 'info',
      req_id: c.get('requestId'),
      method: c.req.method,
      path: c.req.path,
      status: c.res.status,
      ms: Math.round((performance.now() - start) * 10) / 10,
    });
  });
  // CORS * for GET /v0/* (and its preflight); other methods stay same-origin.
  const corsGet = cors({ origin: '*', allowMethods: ['GET'], maxAge: 600 });
  app.use('/v0/*', (c, next) => (c.req.method === 'GET' || c.req.method === 'OPTIONS' ? corsGet(c, next) : next()));
  app.use('/internal/*', bodyLimit({ maxSize: 16 * 1024 * 1024 }));
  app.use('/v0/*', bodyLimit({ maxSize: 256 * 1024 }));
  app.use('*', async (c, next) => {
    if (isApiPath(c.req.path)) {
      c.set(
        'principal',
        await authenticate(
          deps.config,
          { authorization: c.req.header('authorization'), viewer: c.req.header('x-rebox-viewer') },
          deps.db,
        ),
      );
    }
    await next();
  });

  app.get('/health', async (c) => {
    const ok = await deps.db.ping();
    return c.json({ ok, db: ok }, ok ? 200 : 503);
  });
  app.route('/', readRoutes(deps));
  app.route('/', timelineRoutes(deps));
  app.route('/', wikiRoutes({ ...deps, log }));
  app.route('/', internalRoutes(deps));
  app.route('/', adminRoutes(deps));
  app.route('/', workerRoutes(deps));

  const dir = deps.publicDir;
  if (dir && existsSync(dir) && statSync(dir).isDirectory()) {
    const indexPath = join(dir, 'index.html');
    app.use('*', async (c, next) => (isApiPath(c.req.path) ? next() : serveStatic({ root: dir })(c, next)));
    // SPA fallback: any non-API GET that matched no file gets index.html.
    app.get('*', async (c, next) => {
      if (isApiPath(c.req.path) || !existsSync(indexPath)) return next();
      return c.html(await readFile(indexPath, 'utf8'));
    });
  }

  app.notFound(() => {
    throw notFound('no such route');
  });
  app.onError((err, c) => {
    if (err instanceof ApiError) return c.json(err.body(), err.status);
    const e = err as { status?: number; message?: string };
    // Hono's own HTTPExceptions (e.g. body-limit 413).
    if (typeof e.status === 'number' && e.status >= 400 && e.status < 500) {
      return c.json({ error: 'http', message: e.message ?? 'bad request' }, e.status as 413);
    }
    log({ at: new Date().toISOString(), level: 'error', req_id: c.get('requestId'), error: String(err), stack: (err as Error).stack });
    return c.json({ error: 'internal', message: 'internal error', request_id: c.get('requestId') }, 500);
  });

  return app;
}
