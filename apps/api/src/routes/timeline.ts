/** GET /v0/entities/:id/timeline (CONTRACTS §14): dated history, newest first, paginated. */
import { Hono } from 'hono';
import type { AppEnv, Deps } from '../app.js';
import { readView } from '../auth.js';
import { decodeCursor, encodeCursor } from '../cursor.js';
import { canonicalIds, getEntity } from '../entities.js';
import { badRequest, notFound } from '../errors.js';
import { buildTimeline, compareEvents, type TimelineEvent } from '../timeline.js';

interface EventCursor {
  at: string;
  key: string;
}

const isEventCursor = (v: unknown): v is EventCursor =>
  typeof v === 'object' && v !== null && typeof (v as EventCursor).at === 'string' && typeof (v as EventCursor).key === 'string';

export function intParam(raw: string | undefined, name: string, def: number, max: number): number {
  if (raw === undefined || raw === '') return def;
  if (!/^\d+$/.test(raw)) throw badRequest(`${name} must be an integer`);
  const n = Number(raw);
  if (n < 1 || n > max) throw badRequest(`${name} must be between 1 and ${max}`);
  return n;
}

/** Events strictly after the cursor in timeline order, then one page of them. */
export function pageEvents(events: TimelineEvent[], after: EventCursor | null, limit: number) {
  const rest = after ? events.filter((e) => compareEvents(e, after) > 0) : events;
  const page = rest.slice(0, limit);
  const last = page[page.length - 1];
  return { events: page, next: rest.length > limit && last ? encodeCursor({ at: last.at, key: last.key }) : null };
}

export function timelineRoutes(deps: Deps) {
  const { db, vocab } = deps;
  const app = new Hono<AppEnv>();

  app.get('/v0/entities/:id/timeline', async (c) => {
    const id = c.req.param('id');
    const limit = intParam(c.req.query('limit'), 'limit', 100, 500);
    const after = decodeCursor(c.req.query('cursor'), isEventCursor);
    const [v, voc] = await Promise.all([readView(db, c.get('principal')), vocab.get()]);
    const body = await db.withView(v, async (tx) => {
      const canon = (await canonicalIds(tx, [id])).get(id);
      const entity = canon ? await getEntity(tx, canon) : null;
      if (!canon || !entity) throw notFound(`entity ${id} not found`);
      const events = await buildTimeline(tx, canon, entity.kind, voc);
      return { id: canon, ...pageEvents(events, after, limit) };
    });
    return c.json(body);
  });

  return app;
}
