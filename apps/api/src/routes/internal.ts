/** Claims writer endpoint (CONTRACTS §5, §12): the ingest token or an external worker's token. */
import { Hono } from 'hono';
import type { AppEnv, Deps } from '../app.js';
import { requireRole } from '../auth.js';
import { badRequest } from '../errors.js';
import type { Actor } from '../writer.js';

export function internalRoutes(deps: Deps) {
  const app = new Hono<AppEnv>();
  app.post('/internal/claims', async (c) => {
    const p = c.get('principal');
    requireRole(p, 'ingest', 'worker');
    const body = await c.req.json().catch(() => {
      throw badRequest('body must be JSON');
    });
    const actor: Actor = p.role === 'worker' ? { kind: 'worker', workerId: p.workerId! } : { kind: 'ingest' };
    return c.json(await deps.writer.write(body, actor));
  });
  return app;
}
