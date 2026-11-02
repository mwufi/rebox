/**
 * Admin API v0 (CONTRACTS §7): review queue, merge, split, dismiss. Admin token only.
 *
 * Every write runs in one transaction under the admin view (all public layers visible and
 * writable), locks the entity rows it changes, and records the decision twice: in `merge_log`
 * (audit) and in `change` (op `merge` / `split`, layer_id null, subject = the merged-away id,
 * object_id = the target), so change-feed consumers see merges in commit order.
 */
import { Hono } from 'hono';
import { z } from 'zod';
import type { AppEnv, Deps } from '../app.js';
import { adminView, requireRole } from '../auth.js';
import type { Tx } from '../db.js';
import { entityRefs, type EntityRef } from '../entities.js';
import { badRequest, notFound } from '../errors.js';

/** Admin tokens carry no identity in v0; merge_log.actor and review_item.resolved_by use this. */
const ACTOR = 'admin';
const STATUSES = ['open', 'merged', 'dismissed'] as const;
const MAX_REVIEW = 200;

export interface ReviewItem {
  id: number;
  kind: string;
  entity_ids: string[];
  reason: string;
  evidence: unknown;
  status: (typeof STATUSES)[number];
  resolved_by: string | null;
  created_at: string;
  resolved_at: string | null;
  /** EntityRefs for entity_ids, in the same order; ids that no longer exist are omitted. */
  candidates: EntityRef[];
}

interface ReviewRow {
  id: string;
  kind: string;
  entity_ids: string[];
  reason: string;
  evidence: unknown;
  status: ReviewItem['status'];
  resolved_by: string | null;
  created_at: Date;
  resolved_at: Date | null;
}

const entityId = z.string().trim().min(1).max(200);
const mergeBody = z
  .object({ from: entityId, into: entityId, review_id: z.union([z.number().int().positive(), z.string().regex(/^\d+$/)]).nullish() })
  .strict();
const splitBody = z.object({ id: entityId }).strict();

async function jsonBody(c: { req: { json(): Promise<unknown> } }): Promise<unknown> {
  return c.req.json().catch(() => {
    throw badRequest('body must be JSON');
  });
}

function parse<T>(schema: z.ZodType<T>, body: unknown): T {
  const r = schema.safeParse(body);
  if (!r.success) throw badRequest(r.error.issues.map((i) => (i.path.length ? `${i.path.join('.')}: ${i.message}` : i.message)).join('; '));
  return r.data;
}

async function toItems(tx: Tx, rows: ReviewRow[]): Promise<ReviewItem[]> {
  const refs = await entityRefs(tx, rows.flatMap((r) => r.entity_ids));
  return rows.map((r) => ({
    id: Number(r.id),
    kind: r.kind,
    entity_ids: r.entity_ids,
    reason: r.reason,
    evidence: r.evidence,
    status: r.status,
    resolved_by: r.resolved_by,
    created_at: r.created_at.toISOString(),
    resolved_at: r.resolved_at ? r.resolved_at.toISOString() : null,
    candidates: r.entity_ids.flatMap((id) => {
      const ref = refs.get(id);
      return ref ? [ref] : [];
    }),
  }));
}

/** Locks entity rows (in id order, so concurrent merges cannot deadlock) and returns them by id. */
async function lockEntities(tx: Tx, ids: string[]) {
  const rows = await tx<{ id: string; kind: string; merged_into: string | null }[]>`
    select id, kind, merged_into from entity where id = any(${ids}::text[]) order by id for update`;
  return new Map(rows.map((r) => [r.id, r]));
}

async function lockReview(tx: Tx, id: string) {
  const [row] = await tx<ReviewRow[]>`select * from review_item where id = ${id}::bigint for update`;
  if (!row) throw notFound(`review item ${id} not found`);
  return row;
}

async function emitChange(tx: Tx, op: 'merge' | 'split', subject: string, objectId: string | null): Promise<number> {
  const [row] = await tx<{ seq: string }[]>`
    insert into change (op, layer_id, subject, object_id) values (${op}, null, ${subject}, ${objectId})
    returning seq::text as seq`;
  return Number(row!.seq);
}

export function adminRoutes(deps: Deps) {
  const { db } = deps;
  const app = new Hono<AppEnv>();
  const admin = async (c: { get(key: 'principal'): AppEnv['Variables']['principal'] }) => {
    requireRole(c.get('principal'), 'admin');
    return adminView(db);
  };

  app.get('/v0/review', async (c) => {
    const view = await admin(c);
    const status = c.req.query('status') || 'open';
    if (!(STATUSES as readonly string[]).includes(status)) throw badRequest(`status must be one of ${STATUSES.join(', ')}`);
    const items = await db.withView(view, async (tx) => {
      const rows = await tx<ReviewRow[]>`
        select * from review_item where status = ${status}
        order by created_at desc, id desc limit ${MAX_REVIEW}`;
      return toItems(tx, rows);
    });
    return c.json(items);
  });

  app.post('/v0/admin/merge', async (c) => {
    const view = await admin(c);
    const body = parse(mergeBody, await jsonBody(c));
    const { from, into } = body;
    const reviewId = body.review_id == null ? null : String(body.review_id);
    if (from === into) throw badRequest('cannot merge an entity into itself');

    const result = await db.withView(view, async (tx) => {
      const locked = await lockEntities(tx, [from, into]);
      const src = locked.get(from);
      const dst = locked.get(into);
      if (!src) throw notFound(`entity ${from} not found`);
      if (!dst) throw notFound(`entity ${into} not found`);
      if (src.merged_into) throw badRequest(`${from} is already merged into ${src.merged_into}; split it first`);
      // `into` must be a chain root. Since roots point nowhere, the new edge from -> into can never
      // close a cycle (into's chain is just `into`, which is not `from`).
      if (dst.merged_into) throw badRequest(`${into} is itself merged into ${dst.merged_into}; merge into that entity instead`);
      if (src.kind !== dst.kind) throw badRequest(`cannot merge a ${src.kind} into a ${dst.kind}`);

      let review: ReviewRow | null = null;
      if (reviewId) {
        review = await lockReview(tx, reviewId);
        if (review.status !== 'open') throw badRequest(`review item ${reviewId} is already ${review.status}`);
        if (!review.entity_ids.includes(from)) throw badRequest(`${from} is not a candidate of review item ${reviewId}`);
      }

      await tx`update entity set merged_into = ${into} where id = ${from}`;
      await tx`insert into merge_log (op, from_id, into_id, actor) values ('merge', ${from}, ${into}, ${ACTOR})`;
      if (review) {
        await tx`update review_item set status = 'merged', resolved_by = ${ACTOR}, resolved_at = now() where id = ${reviewId}::bigint`;
      }
      const seq = await emitChange(tx, 'merge', from, into);
      return { merged: { from, into }, review_id: review ? Number(review.id) : null, seq };
    });
    return c.json(result);
  });

  app.post('/v0/admin/split', async (c) => {
    const view = await admin(c);
    const { id } = parse(splitBody, await jsonBody(c));
    const result = await db.withView(view, async (tx) => {
      const row = (await lockEntities(tx, [id])).get(id);
      if (!row) throw notFound(`entity ${id} not found`);
      if (!row.merged_into) throw badRequest(`${id} is not merged`);
      const was = row.merged_into;
      await tx`update entity set merged_into = null where id = ${id}`;
      await tx`insert into merge_log (op, from_id, into_id, actor) values ('split', ${id}, ${was}, ${ACTOR})`;
      const seq = await emitChange(tx, 'split', id, was);
      return { split: { id, from: was }, seq };
    });
    return c.json(result);
  });

  app.post('/v0/admin/review/:id/dismiss', async (c) => {
    const view = await admin(c);
    const id = c.req.param('id');
    if (!/^\d{1,18}$/.test(id)) throw badRequest('review id must be an integer');
    const item = await db.withView(view, async (tx) => {
      const row = await lockReview(tx, id);
      if (row.status !== 'open') throw badRequest(`review item ${id} is already ${row.status}`);
      const [updated] = await tx<ReviewRow[]>`
        update review_item set status = 'dismissed', resolved_by = ${ACTOR}, resolved_at = now()
        where id = ${id}::bigint returning *`;
      return (await toItems(tx, [updated!]))[0]!;
    });
    return c.json(item);
  });

  return app;
}
