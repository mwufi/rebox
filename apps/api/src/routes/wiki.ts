/**
 * GET/POST /v0/entities/:id/wiki (CONTRACTS §14).
 * GET: the cached article (+ `stale`) or 404. POST: generates (or regenerates when stale, or
 * with `?force=true`); admin token required unless WIKI_PUBLIC=true; rate-limited (429).
 */
import { Hono } from 'hono';
import type { AppEnv, Deps } from '../app.js';
import { forbidden, requireRole } from '../auth.js';
import { ApiError, notFound } from '../errors.js';
import { createLimiter, createWikiService, RateLimited, type Wiki } from '../wiki/service.js';

/** Response body: §14 fields first; `sources` lists the cited footnotes. */
const body = (w: Wiki, gen: { can_generate: boolean; public: boolean }) => ({
  content_md: w.content_md,
  model: w.model,
  generated_at: w.generated_at,
  input_seq: w.input_seq,
  stale: w.stale,
  entity_id: w.entity_id,
  sources: w.sources,
  ...gen,
});

export function wikiRoutes(deps: Deps) {
  const app = new Hono<AppEnv>();
  const settings = deps.wiki ?? { model: null, public: false, rateLimit: { max: 6, windowMs: 60_000 } };
  const service = settings.model
    ? createWikiService({
        db: deps.db,
        vocab: deps.vocab,
        model: settings.model,
        limiter: createLimiter(settings.rateLimit.max, settings.rateLimit.windowMs),
        log: deps.log,
      })
    : null;

  // Reading needs no model: a read-only service when generation is disabled.
  const reader =
    service ??
    createWikiService({
      db: deps.db,
      vocab: deps.vocab,
      model: { id: 'none', complete: () => Promise.reject(new Error('wiki generation is disabled')) },
      limiter: { acquire: () => {} },
    });

  // Lets the explorer decide whether to offer Generate / Regenerate (and whether it needs a token).
  const gen = { can_generate: service !== null, public: settings.public };

  app.get('/v0/entities/:id/wiki', async (c) => {
    const id = c.req.param('id');
    const w = await reader.get(id);
    if (w === 'missing') throw notFound(`entity ${id} not found`);
    if (!w) return c.json({ error: 'not_found', message: 'no wiki for this entity yet', ...gen }, 404);
    return c.json(body(w, gen));
  });

  app.post('/v0/entities/:id/wiki', async (c) => {
    const p = c.get('principal');
    if (!settings.public) requireRole(p, 'admin');
    else if (p.role === 'ingest') throw forbidden('use the admin token or no token');
    if (!service) throw new ApiError(503, 'unavailable', 'wiki generation is disabled (no ANTHROPIC_API_KEY)');
    const id = c.req.param('id');
    const force = c.req.query('force') === 'true';
    try {
      const w = await service.generate(id, { force, by: p.role === 'admin' ? 'api:admin' : 'api:public' });
      if (w === 'missing') throw notFound(`entity ${id} not found`);
      return c.json(body(w, gen));
    } catch (err) {
      if (err instanceof RateLimited) {
        c.header('Retry-After', String(err.retryAfterS));
        return c.json({ error: 'rate_limited', message: err.message, retry_after: err.retryAfterS }, 429);
      }
      if (err instanceof ApiError) throw err;
      deps.log?.({ at: new Date().toISOString(), level: 'error', msg: 'wiki generation failed', entity: id, error: String(err) });
      return c.json({ error: 'upstream', message: 'the model call failed; try again later' }, 502);
    }
  });

  return app;
}
