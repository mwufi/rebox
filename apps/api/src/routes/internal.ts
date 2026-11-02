/** Claims writer endpoint (CONTRACTS §5). */
import { Hono } from 'hono';
import type { AppEnv, Deps } from '../app.js';
import { requireRole } from '../auth.js';
import { badRequest } from '../errors.js';

export function internalRoutes(deps: Deps) {
  const app = new Hono<AppEnv>();
  app.post('/internal/claims', async (c) => {
    requireRole(c.get('principal'), 'ingest');
    const body = await c.req.json().catch(() => {
      throw badRequest('body must be JSON');
    });
    return c.json(await deps.writer.write(body));
  });
  return app;
}
