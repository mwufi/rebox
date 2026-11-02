/**
 * Workers and runs (CONTRACTS §12, §14).
 *
 * Internal (ingest token; an external worker token may manage its own runs):
 *   POST  /internal/workers       register (idempotent on name+kind+source) → {id}
 *   POST  /internal/runs          open a run → {run_id}
 *   PATCH /internal/runs/:id      status / stats (merged) / error
 * Admin:
 *   POST  /v0/admin/workers       create an external worker → {id, token} (token shown once)
 *   PATCH /v0/admin/workers/:id   trust / status / description / layers
 * Read (anonymous):
 *   GET /v0/workers, GET /v0/workers/:id, GET /v0/stats/timeseries
 *
 * Read stats come from `change` (which carries worker_id/run_id since 0005), `run` and `worker`,
 * never from scanning `claim`. Like /v0/changes, only change rows in the viewer's visible layers
 * count, and only workers with a public or visible layer are listed, so private activity never
 * leaks through the counts.
 */
import { randomInt } from 'node:crypto';
import { Hono } from 'hono';
import { z } from 'zod';
import type { AppEnv, Deps } from '../app.js';
import { forbidden, newWorkerToken, readView, requireRole, type Principal } from '../auth.js';
import { NO_VIEW, type Tx } from '../db.js';
import { badRequest, conflict, notFound } from '../errors.js';
import { claimFacts } from '../views.js';
import { parseLayer, WORKER_ID_RE, WORKER_KINDS, type WorkerKind } from '../writer.js';

export const DEFAULT_TRUST: Record<WorkerKind, number> = {
  connector: 0.8,
  spider: 0.8,
  parser: 0.6,
  enricher: 0.5,
  external: 0.3,
};
const RUN_KINDS = ['sync', 'replay', 'parse', 'enrich', 'wiki'] as const;
const RUN_STATUSES = ['running', 'ok', 'failed'] as const;
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,40}$/;

const layerId = z.string().refine((s) => parseLayer(s) !== null, 'must be a layer id (public/<source>, ...)');
const registerBody = z
  .object({
    name: z.string().regex(NAME_RE, 'must match [a-z0-9][a-z0-9-]*'),
    kind: z.enum(WORKER_KINDS),
    source: z.string().min(1).max(100),
    layers: z.array(layerId).min(1).max(50),
    config: z.record(z.string(), z.unknown()).nullish(),
    description: z.string().max(2000).nullish(),
    id: z.string().regex(WORKER_ID_RE).nullish(),
  })
  .strict();
const adminCreateBody = registerBody
  .omit({ id: true, kind: true })
  .extend({
    kind: z.enum(WORKER_KINDS).default('external'),
    owner: z.string().regex(/^[a-z0-9][a-z0-9_.-]{0,63}$/).refine((o) => o !== 'rebox', 'owner "rebox" is reserved for internal workers'),
    trust: z.number().min(0).max(1).nullish(),
  })
  .strict();
const adminPatchBody = z
  .object({
    trust: z.number().min(0).max(1).optional(),
    status: z.enum(['active', 'paused', 'revoked']).optional(),
    description: z.string().max(2000).nullable().optional(),
    layers: z.array(layerId).min(1).max(50).optional(),
  })
  .strict();
const runBody = z.object({ worker: z.string().regex(WORKER_ID_RE), kind: z.enum(RUN_KINDS) }).strict();
const runPatchBody = z
  .object({
    status: z.enum(RUN_STATUSES).optional(),
    // Pipeline counters plus whatever the connector reports (x_stop, failed_queries, ...): any JSON,
    // capped in size.
    stats: z
      .record(z.string().max(100), z.unknown())
      .refine((o) => Object.keys(o).length <= 100 && JSON.stringify(o).length <= 32_000, 'stats too large')
      .optional(),
    error: z.string().max(4000).nullable().optional(),
  })
  .strict();

function parse<T>(schema: z.ZodType<T>, body: unknown): T {
  const r = schema.safeParse(body);
  if (!r.success) throw badRequest(r.error.issues.map((i) => (i.path.length ? `${i.path.join('.')}: ${i.message}` : i.message)).join('; '));
  return r.data;
}

async function jsonBody(c: { req: { json(): Promise<unknown> } }): Promise<unknown> {
  return c.req.json().catch(() => {
    throw badRequest('body must be JSON');
  });
}

interface WorkerRow {
  id: string;
  kind: string;
  source: string;
  owner: string;
  trust: number;
  layers: string[];
  description: string | null;
  config: Record<string, unknown>;
  status: string;
  created_at: Date;
  last_seen_at: Date | null;
}

const WORKER_COLS = (tx: Tx) =>
  tx`w.id, w.kind, w.source, w.owner, w.trust, w.layers, w.description, w.config, w.status, w.created_at, w.last_seen_at`;

const iso = (d: Date | null | undefined) => (d ? d.toISOString() : null);

function workerJson(w: WorkerRow) {
  return {
    id: w.id,
    kind: w.kind,
    source: w.source,
    owner: w.owner,
    trust: Number(w.trust),
    status: w.status,
    layers: w.layers,
    description: w.description,
    config: w.config,
    created_at: iso(w.created_at),
    last_seen_at: iso(w.last_seen_at),
  };
}

interface RunRow {
  id: string;
  worker_id: string;
  source: string;
  kind: string;
  status: string;
  started_at: Date;
  finished_at: Date | null;
  stats: Record<string, unknown>;
  error: string | null;
}

function runJson(r: RunRow) {
  const end = r.finished_at ?? null;
  return {
    id: Number(r.id),
    worker: r.worker_id,
    source: r.source,
    kind: r.kind,
    status: r.status,
    started_at: iso(r.started_at),
    finished_at: iso(end),
    duration_s: end ? Math.round((end.getTime() - r.started_at.getTime()) / 100) / 10 : null,
    stats: r.stats,
    error: r.error,
  };
}

/** `<name>-<kind>-<6 random digits>` that is not taken yet. */
async function freshWorkerId(tx: Tx, name: string, kind: string): Promise<string> {
  for (let i = 0; i < 20; i++) {
    const id = `${name}-${kind}-${String(randomInt(0, 1_000_000)).padStart(6, '0')}`;
    const [taken] = await tx`select 1 from worker where id = ${id}`;
    if (!taken) return id;
  }
  throw conflict(`could not find a free id for ${name}-${kind}`);
}

/** Internal routes may act for internal workers (ingest) or for the caller's own external worker. */
function checkActsFor(p: Principal, worker: { id: string; owner: string }) {
  if (p.role === 'ingest' && worker.owner !== 'rebox') throw forbidden(`worker ${worker.id} is external; use its own token`);
  if (p.role === 'worker' && p.workerId !== worker.id) throw forbidden(`this token may only act as worker ${p.workerId}`);
}

/** Visible-layer filter for change rows (no RLS on `change`). */
const visibleChange = (tx: Tx, alias = 'ch') => tx`${tx(alias)}.layer_id = any(rebox_visible())`;

/** Workers with any public or visible layer (private-only workers stay hidden from others). */
const visibleWorker = (tx: Tx) =>
  tx`exists (select 1 from unnest(w.layers) l where l like 'public/%' or l = any(rebox_visible()))`;

// ── timeseries ────────────────────────────────────────────────────────────

const TS_METRICS = ['entities', 'claims'] as const;
const TS_BY = ['kind', 'worker', 'layer'] as const;
const TS_BUCKETS = { hour: { def: 48 * 3600e3, max: 31 * 86400e3 }, day: { def: 90 * 86400e3, max: 3 * 366 * 86400e3 } } as const;
const TS_CACHE_MS = 30_000;

export interface TimeseriesQuery {
  metric: (typeof TS_METRICS)[number];
  by: (typeof TS_BY)[number];
  bucket: keyof typeof TS_BUCKETS;
  since: Date;
}

export function parseTimeseries(q: Record<string, string | undefined>, now = Date.now()): TimeseriesQuery {
  const metric = (q.metric || 'claims') as TimeseriesQuery['metric'];
  const by = (q.by || 'kind') as TimeseriesQuery['by'];
  const bucket = (q.bucket || 'hour') as TimeseriesQuery['bucket'];
  if (!TS_METRICS.includes(metric)) throw badRequest(`metric must be one of ${TS_METRICS.join(', ')}`);
  if (!TS_BY.includes(by)) throw badRequest(`by must be one of ${TS_BY.join(', ')}`);
  if (!(bucket in TS_BUCKETS)) throw badRequest('bucket must be hour or day');
  const lim = TS_BUCKETS[bucket];
  let since = new Date(now - lim.def);
  if (q.since) {
    const ms = Date.parse(q.since);
    if (!/^\d{4}-\d{2}-\d{2}/.test(q.since) || Number.isNaN(ms)) throw badRequest('since must be an ISO date or timestamp');
    if (now - ms > lim.max) throw badRequest(`since is too far back for bucket=${bucket} (max ${Math.round(lim.max / 86400e3)} days)`);
    since = new Date(ms);
  }
  return { metric, by, bucket, since };
}

/**
 * Buckets from the change feed. claims = claim writes (inserts and updates: a new claim, a changed
 * value, or a pre-0005 claim attributed to the worker that re-sent it); entities = the first insert per subject
 * (the moment the entity first appeared in the graph), counted in the bucket where it happened.
 * Keys: kind = the id prefix (rbx:<kind>:...), worker = worker id ("unattributed" for writes
 * before migration 0005), layer = layer id.
 */
async function timeseries(tx: Tx, q: TimeseriesQuery) {
  const keyExpr =
    q.by === 'kind'
      ? tx`split_part(subject, ':', 2)`
      : q.by === 'worker'
        ? tx`coalesce(worker_id, 'unattributed')`
        : tx`layer_id`;
  const bucket = q.bucket;
  const rows =
    q.metric === 'claims'
      ? await tx<{ t: Date; key: string; n: number }[]>`
          select date_trunc(${bucket}, at) as t, ${keyExpr} as key, count(*)::int as n
          from change ch
          where ch.op in ('insert', 'update') and ch.at >= ${q.since} and ${visibleChange(tx)}
          group by 1, 2 order by 1, 2`
      : await tx<{ t: Date; key: string; n: number }[]>`
          with firsts as (
            select distinct on (subject) subject, at, worker_id, layer_id
            from change ch
            where ch.op = 'insert' and ch.subject is not null and ${visibleChange(tx)}
            order by subject, seq
          )
          select date_trunc(${bucket}, at) as t, ${keyExpr} as key, count(*)::int as n
          from firsts where at >= ${q.since}
          group by 1, 2 order by 1, 2`;
  return { buckets: rows.map((r) => ({ t: r.t.toISOString(), key: r.key, n: r.n })) };
}

// ── routes ────────────────────────────────────────────────────────────────

export function workerRoutes(deps: Deps) {
  const { db } = deps;
  const app = new Hono<AppEnv>();
  const tsCache = new Map<string, { at: number; value: Promise<unknown> }>();

  app.post('/internal/workers', async (c) => {
    requireRole(c.get('principal'), 'ingest');
    const b = parse(registerBody, await jsonBody(c));
    if (b.kind === 'external') throw badRequest('external workers are created with POST /v0/admin/workers');
    if (b.id && !b.id.startsWith(`${b.name}-${b.kind}-`)) throw badRequest(`id must be ${b.name}-${b.kind}-<6 digits>`);
    const result = await db.withView(NO_VIEW, async (tx) => {
      // Serialize registrations of the same identity so concurrent first runs agree on one id.
      await tx`select pg_advisory_xact_lock(hashtext(${`rebox-worker:${b.name}|${b.kind}|${b.source}`}))`;
      const [existing] = b.id
        ? await tx<WorkerRow[]>`select ${WORKER_COLS(tx)} from worker w where w.id = ${b.id}`
        : await tx<WorkerRow[]>`
            select ${WORKER_COLS(tx)} from worker w
            where w.kind = ${b.kind} and w.source = ${b.source} and w.owner = 'rebox'
              and w.id ~ ${`^${b.name}-${b.kind}-[0-9]{6}$`}
            order by w.created_at, w.id limit 1`;
      if (existing) {
        if (existing.owner !== 'rebox' || existing.kind !== b.kind || existing.source !== b.source) {
          throw conflict(`worker ${existing.id} exists with a different kind, source or owner`);
        }
        // Re-registering may add layers (a connector that starts writing a second layer).
        const missing = b.layers.filter((l) => !existing.layers.includes(l));
        if (missing.length) await tx`update worker set layers = layers || ${missing}::text[] where id = ${existing.id}`;
        return { id: existing.id, created: false };
      }
      const id = b.id ?? (await freshWorkerId(tx, b.name, b.kind));
      await tx`
        insert into worker (id, kind, source, owner, trust, layers, description, config)
        values (${id}, ${b.kind}, ${b.source}, 'rebox', ${DEFAULT_TRUST[b.kind]}, ${b.layers}::text[],
                ${b.description ?? null}, ${tx.json((b.config ?? {}) as never)})`;
      return { id, created: true };
    });
    return c.json(result, result.created ? 201 : 200);
  });

  app.post('/internal/runs', async (c) => {
    const p = c.get('principal');
    requireRole(p, 'ingest', 'worker');
    const b = parse(runBody, await jsonBody(c));
    const run = await db.withView(NO_VIEW, async (tx) => {
      const [w] = await tx<{ id: string; owner: string; source: string; status: string }[]>`
        select id, owner, source, status from worker where id = ${b.worker}`;
      if (!w) throw notFound(`worker ${b.worker} not found`);
      checkActsFor(p, w);
      if (w.status !== 'active') throw badRequest(`worker ${w.id} is ${w.status}, not active`);
      const [r] = await tx<{ id: string }[]>`
        insert into run (worker_id, source, kind) values (${w.id}, ${w.source}, ${b.kind}) returning id::text as id`;
      await tx`update worker set last_seen_at = now() where id = ${w.id}`;
      return Number(r!.id);
    });
    return c.json({ run_id: run }, 201);
  });

  app.patch('/internal/runs/:id', async (c) => {
    const p = c.get('principal');
    requireRole(p, 'ingest', 'worker');
    const id = c.req.param('id');
    if (!/^\d{1,18}$/.test(id)) throw badRequest('run id must be an integer');
    const b = parse(runPatchBody, await jsonBody(c));
    const run = await db.withView(NO_VIEW, async (tx) => {
      const [cur] = await tx<(RunRow & { owner: string })[]>`
        select r.*, r.id::text as id, w.owner from run r join worker w on w.id = r.worker_id
        where r.id = ${id}::bigint for update of r`;
      if (!cur) throw notFound(`run ${id} not found`);
      checkActsFor(p, { id: cur.worker_id, owner: cur.owner });
      if (cur.status !== 'running' && b.status === 'running') throw badRequest(`run ${id} is already ${cur.status}`);
      const finishing = b.status !== undefined && b.status !== 'running';
      const [r] = await tx<RunRow[]>`
        update run set
          stats = stats || ${tx.json((b.stats ?? {}) as never)}::jsonb,
          status = coalesce(${b.status ?? null}, status),
          error = case when ${b.error !== undefined} then ${b.error ?? null} else error end,
          finished_at = case when ${finishing} then coalesce(finished_at, now()) else finished_at end
        where id = ${id}::bigint
        returning *, id::text as id`;
      await tx`update worker set last_seen_at = now() where id = ${cur.worker_id}`;
      return r!;
    });
    return c.json(runJson(run));
  });

  app.post('/v0/admin/workers', async (c) => {
    requireRole(c.get('principal'), 'admin');
    const b = parse(adminCreateBody, await jsonBody(c));
    const out = await db.withView(NO_VIEW, async (tx) => {
      const id = await freshWorkerId(tx, b.name, b.kind);
      const { token, hash } = newWorkerToken(id);
      const [w] = await tx<WorkerRow[]>`
        insert into worker (id, kind, source, owner, trust, layers, description, config, token_hash)
        values (${id}, ${b.kind}, ${b.source}, ${b.owner}, ${b.trust ?? DEFAULT_TRUST[b.kind]}, ${b.layers}::text[],
                ${b.description ?? null}, ${tx.json((b.config ?? {}) as never)}, ${hash})
        returning *`;
      return { ...workerJson(w!), token };
    });
    return c.json(out, 201);
  });

  app.patch('/v0/admin/workers/:id', async (c) => {
    requireRole(c.get('principal'), 'admin');
    const id = c.req.param('id');
    const b = parse(adminPatchBody, await jsonBody(c));
    const w = await db.withView(NO_VIEW, async (tx) => {
      const [row] = await tx<WorkerRow[]>`
        update worker set
          trust = coalesce(${b.trust ?? null}::real, trust),
          status = coalesce(${b.status ?? null}, status),
          description = case when ${b.description !== undefined} then ${b.description ?? null} else description end,
          layers = coalesce(${b.layers ?? null}::text[], layers)
        where id = ${id}
        returning *`;
      if (!row) throw notFound(`worker ${id} not found`);
      return row;
    });
    return c.json(workerJson(w));
  });

  app.get('/v0/workers', async (c) => {
    const v = await readView(db, c.get('principal'));
    const rows = await db.withView(v, async (tx) => {
      const workers = await tx<(WorkerRow & { runs_ok: number; runs_failed: number })[]>`
        select ${WORKER_COLS(tx)},
               (select count(*)::int from run r where r.worker_id = w.id and r.status = 'ok') as runs_ok,
               (select count(*)::int from run r where r.worker_id = w.id and r.status = 'failed') as runs_failed
        from worker w where ${visibleWorker(tx)}
        order by w.last_seen_at desc nulls last, w.id`;
      if (!workers.length) return [];
      const ids = workers.map((w) => w.id);
      const [counts, lastRuns] = await Promise.all([
        tx<{ worker_id: string; total: number; day: number }[]>`
          select ch.worker_id,
                 count(distinct ch.claim_id)::int as total,
                 count(*) filter (where ch.at > now() - interval '24 hours')::int as day
          from change ch
          where ch.worker_id = any(${ids}::text[]) and ch.op in ('insert', 'update') and ${visibleChange(tx)}
          group by ch.worker_id`,
        tx<RunRow[]>`
          select distinct on (r.worker_id) r.*, r.id::text as id from run r
          where r.worker_id = any(${ids}::text[])
          order by r.worker_id, r.started_at desc, r.id desc`,
      ]);
      const countOf = new Map(counts.map((r) => [r.worker_id, r]));
      const lastOf = new Map(lastRuns.map((r) => [r.worker_id, r]));
      return workers.map((w) => {
        const last = lastOf.get(w.id);
        return {
          id: w.id,
          kind: w.kind,
          source: w.source,
          owner: w.owner,
          trust: Number(w.trust),
          status: w.status,
          description: w.description,
          last_seen_at: iso(w.last_seen_at),
          claims_total: countOf.get(w.id)?.total ?? 0,
          claims_24h: countOf.get(w.id)?.day ?? 0,
          runs_ok: w.runs_ok,
          runs_failed: w.runs_failed,
          last_run: last ? runJson(last) : null,
        };
      });
    });
    return c.json(rows);
  });

  app.get('/v0/workers/:id', async (c) => {
    const id = c.req.param('id');
    const v = await readView(db, c.get('principal'));
    const body = await db.withView(v, async (tx) => {
      const [w] = await tx<WorkerRow[]>`select ${WORKER_COLS(tx)} from worker w where w.id = ${id} and ${visibleWorker(tx)}`;
      if (!w) throw notFound(`worker ${id} not found`);
      const [runs, throughput, predicates, recent, [totals]] = await Promise.all([
        tx<RunRow[]>`select r.*, r.id::text as id from run r where r.worker_id = ${id} order by r.started_at desc, r.id desc limit 50`,
        tx<{ t: Date; n: number }[]>`
          select date_trunc('hour', ch.at) as t, count(*)::int as n from change ch
          where ch.worker_id = ${id} and ch.at > now() - interval '48 hours'
            and ch.op in ('insert', 'update') and ${visibleChange(tx)}
          group by 1 order by 1`,
        tx<{ predicate: string; n: number }[]>`
          select ch.predicate, count(distinct ch.claim_id)::int as n
          from change ch
          where ch.worker_id = ${id} and ch.op in ('insert', 'update') and ${visibleChange(tx)}
          group by 1 order by 2 desc, 1`,
        tx<{ claim_id: string }[]>`
          select max(ch.claim_id)::text as claim_id from (
            select claim_id, seq from change ch
            where ch.worker_id = ${id} and ch.op in ('insert', 'update') and ${visibleChange(tx)}
            order by ch.seq desc limit 100
          ) ch group by ch.claim_id order by max(ch.seq) desc limit 20`,
        tx<{ total: number; day: number; runs_ok: number; runs_failed: number }[]>`
          select (select count(distinct ch.claim_id)::int from change ch
                   where ch.worker_id = ${id} and ch.op in ('insert','update') and ${visibleChange(tx)}) as total,
                 (select count(*)::int from change ch where ch.worker_id = ${id} and ch.op in ('insert','update')
                    and ch.at > now() - interval '24 hours' and ${visibleChange(tx)}) as day,
                 (select count(*)::int from run where worker_id = ${id} and status = 'ok') as runs_ok,
                 (select count(*)::int from run where worker_id = ${id} and status = 'failed') as runs_failed`,
      ]);
      const facts = await claimFacts(tx, recent.map((r) => r.claim_id));
      // Zero-filled hourly series, oldest first, ending at the current hour.
      const byHour = new Map(throughput.map((r) => [r.t.getTime(), r.n]));
      const nowHour = Math.floor(Date.now() / 3600e3) * 3600e3;
      const series = Array.from({ length: 48 }, (_, i) => {
        const t = nowHour - (47 - i) * 3600e3;
        return { t: new Date(t).toISOString(), n: byHour.get(t) ?? 0 };
      });
      return {
        ...workerJson(w),
        claims_total: totals!.total,
        claims_24h: totals!.day,
        runs_ok: totals!.runs_ok,
        runs_failed: totals!.runs_failed,
        runs: runs.map(runJson),
        throughput: series,
        predicates,
        recent_facts: facts,
      };
    });
    return c.json(body);
  });

  app.get('/v0/stats/timeseries', async (c) => {
    const q = parseTimeseries({
      metric: c.req.query('metric'),
      by: c.req.query('by'),
      bucket: c.req.query('bucket'),
      since: c.req.query('since'),
    });
    const v = await readView(db, c.get('principal'));
    // The entities metric reads the whole change feed; cache identical requests briefly.
    const key = JSON.stringify([q.metric, q.by, q.bucket, c.req.query('since') ?? '', v.visible]);
    const hit = tsCache.get(key);
    if (hit && Date.now() - hit.at < TS_CACHE_MS) return c.json(await hit.value);
    const value = db.withView(v, (tx) => timeseries(tx, q));
    tsCache.set(key, { at: Date.now(), value });
    value.catch(() => tsCache.delete(key));
    if (tsCache.size > 200) tsCache.delete(tsCache.keys().next().value!);
    return c.json(await value);
  });

  return app;
}
