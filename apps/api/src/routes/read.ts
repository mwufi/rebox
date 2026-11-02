/** Read API v0 (CONTRACTS §6): public, anonymous; dev viewers add their private layers. */
import { Hono } from 'hono';
import { z } from 'zod';
import type { AppEnv, Deps } from '../app.js';
import { readView } from '../auth.js';
import { decodeCursor, encodeCursor } from '../cursor.js';
import { canonicalIds, entityRefs } from '../entities.js';
import { badRequest, notFound } from '../errors.js';
import { foldEdgePage, preferGenericPlans, type EdgeKey } from '../fold.js';
import { planQuery, runQuery } from '../query.js';
import { search } from '../search.js';
import { lookupEntity, toFacts } from '../views.js';

const MAX_IDS = 200;

/** Parses an optional positive integer query param within [1, max]. */
function intParam(raw: string | undefined, name: string, def: number, max: number, min = 1): number {
  if (raw === undefined || raw === '') return def;
  if (!/^\d+$/.test(raw)) throw badRequest(`${name} must be an integer`);
  const n = Number(raw);
  if (n < min || n > max) throw badRequest(`${name} must be between ${min} and ${max}`);
  return n;
}

const isEdgeKey = (v: unknown): v is EdgeKey =>
  typeof v === 'object' &&
  v !== null &&
  typeof (v as EdgeKey).predicate === 'string' &&
  typeof (v as EdgeKey).other === 'string';

export function readRoutes(deps: Deps) {
  const { db, vocab } = deps;
  const app = new Hono<AppEnv>();
  const view = (p: AppEnv['Variables']['principal']) => readView(db, p);

  app.get('/v0/stats', async (c) => {
    const v = await view(c.get('principal'));
    const stats = await db.withView(v, async (tx) => {
      const [kinds, layers, [seq]] = await Promise.all([
        tx<{ kind: string; n: number }[]>`
          select kind, count(*)::int as n from entity where merged_into is null group by kind order by kind`,
        tx<{ id: string; claims: number }[]>`
          select layer_id as id, count(*)::int as claims from claim group by layer_id order by layer_id`,
        tx<{ last_seq: string | null }[]>`select max(seq)::text as last_seq from change`,
      ]);
      return {
        entities: Object.fromEntries(kinds.map((k) => [k.kind, k.n])),
        claims: layers.reduce((n, l) => n + l.claims, 0),
        layers,
        last_seq: Number(seq?.last_seq ?? 0),
      };
    });
    return c.json(stats);
  });

  app.get('/v0/predicates', async (c) => {
    const { predicates } = await vocab.get();
    return c.json(
      [...predicates.values()].map((p) => ({
        id: p.id,
        subject_kinds: p.subject_kinds,
        object_kind: p.object_kind,
        cardinality: p.cardinality,
        meta: p.meta,
      })),
    );
  });

  app.get('/v0/search', async (c) => {
    const q = (c.req.query('q') ?? '').trim();
    if (q.length > 200) throw badRequest('q is too long');
    const kind = c.req.query('kind') || undefined;
    const limit = intParam(c.req.query('limit'), 'limit', 20, 100);
    if (!q) return c.json([]);
    const v = await view(c.get('principal'));
    return c.json(await db.withView(v, (tx) => search(tx, q, { kind, limit })));
  });

  app.get('/v0/entities', async (c) => {
    const raw = c.req.query('ids') ?? '';
    const ids = [...new Set(raw.split(',').map((s) => s.trim()).filter(Boolean))];
    if (ids.length > MAX_IDS) throw badRequest(`at most ${MAX_IDS} ids`);
    if (ids.length === 0) return c.json([]);
    const v = await view(c.get('principal'));
    const refs = await db.withView(v, async (tx) => {
      // Merged ids resolve to their target's ref, in request order; unknown ids are omitted.
      const canon = await canonicalIds(tx, ids);
      const byId = await entityRefs(tx, [...canon.values()]);
      return ids.flatMap((id) => {
        const ref = byId.get(canon.get(id) ?? '');
        return ref ? [ref] : [];
      });
    });
    return c.json(refs);
  });

  app.get('/v0/entities/:id', async (c) => {
    const id = c.req.param('id');
    const v = await view(c.get('principal'));
    const result = await db.withView(v, (tx) => lookupEntity(tx, id));
    if (result.status === 'missing') throw notFound(`entity ${id} not found`);
    if (result.status === 'redirect') return c.json({ redirect: result.redirect, entity: result.view });
    return c.json(result.view);
  });

  app.get('/v0/entities/:id/edges', async (c) => {
    const id = c.req.param('id');
    const dir = c.req.query('dir') ?? 'out';
    if (dir !== 'out' && dir !== 'in') throw badRequest('dir must be out or in');
    const predicate = c.req.query('predicate') || undefined;
    const limit = intParam(c.req.query('limit'), 'limit', 50, 500);
    const after = decodeCursor(c.req.query('cursor'), isEdgeKey);
    if (predicate) {
      const p = (await vocab.get()).predicates.get(predicate);
      if (!p) throw badRequest(`unknown predicate ${JSON.stringify(predicate)}`);
      if (!p.object_kind) throw badRequest(`${predicate} is an attribute, not an edge`);
    }
    const v = await view(c.get('principal'));
    const body = await db.withView(v, async (tx) => {
      await preferGenericPlans(tx);
      const canon = (await canonicalIds(tx, [id])).get(id);
      if (!canon) throw notFound(`entity ${id} not found`);
      // S13: picks the page's keys before folding, so hubs cost O(limit) folds.
      const rows = await foldEdgePage(tx, {
        root: canon,
        dir,
        predicates: predicate ? [predicate] : undefined,
        after,
        limit: limit + 1,
      });
      const page = rows.slice(0, limit);
      const last = page[page.length - 1];
      return {
        edges: await toFacts(tx, page, dir),
        next: rows.length > limit && last ? encodeCursor({ predicate: last.predicate, other: last.other }) : null,
      };
    });
    return c.json(body);
  });

  app.post('/v0/query', async (c) => {
    const body = await c.req.json().catch(() => {
      throw badRequest('body must be JSON');
    });
    const plan = planQuery(body, await vocab.get());
    const v = await view(c.get('principal'));
    return c.json(await db.withView(v, (tx) => runQuery(tx, plan)));
  });

  const changesQuery = z.object({ after: z.string().regex(/^\d+$/).optional() });
  app.get('/v0/changes', async (c) => {
    const q = changesQuery.safeParse({ after: c.req.query('after') || undefined });
    if (!q.success) throw badRequest('after must be a non-negative integer');
    const after = q.data.after ?? '0';
    const limit = intParam(c.req.query('limit'), 'limit', 500, 1000);
    const v = await view(c.get('principal'));
    const body = await db.withView(v, async (tx) => {
      // The change table has no RLS; show only rows for layers this viewer can see (merge/split
      // rows carry no layer and are public).
      const rows = await tx<
        { seq: string; op: string; layer_id: string | null; subject: string | null; predicate: string | null; object_id: string | null; at: Date }[]
      >`
        select ch.seq::text as seq, ch.op, ch.layer_id, ch.subject, ch.predicate, ch.object_id, ch.at
        from change ch
        where ch.seq > ${after}::bigint and (ch.layer_id is null or ch.layer_id = any(rebox_visible()))
        order by ch.seq
        limit ${limit}`;
      const changes = rows.map((r) => ({ ...r, seq: Number(r.seq), at: r.at.toISOString() }));
      return { changes, next: changes.length ? changes[changes.length - 1]!.seq : Number(after) };
    });
    return c.json(body);
  });

  return app;
}
