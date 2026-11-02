/**
 * Wiki articles (CONTRACTS §14): generated from PUBLIC facts only, cached in `entity_wiki`.
 *
 * - `input_seq` = the newest change seq touching the entity (as subject or object, merges
 *   included) when the article was generated. The article is `stale` when a newer visible
 *   public change touches the entity.
 * - Generation is rate-limited per process (sliding window) and single-flight per entity.
 */
import type { Db, Tx, View } from '../db.js';
import { canonicalIds, entityRefs, mergedFrom, type EntityRef } from '../entities.js';
import type { Vocab } from '../vocab.js';
import { finishArticle, gatherWikiInput, wikiPrompt, type WikiSource } from './input.js';
import type { WikiModel } from './model.js';

export interface Wiki {
  entity_id: string;
  content_md: string;
  model: string;
  generated_at: string;
  input_seq: number;
  stale: boolean;
  sources: WikiSource[];
}

interface WikiRow {
  entity_id: string;
  content_md: string;
  model: string;
  input_seq: string;
  sources: WikiSource[];
  generated_at: Date;
}

export class RateLimited extends Error {
  constructor(readonly retryAfterS: number) {
    super('wiki generation rate limit reached');
  }
}

/** Sliding-window limiter: at most `max` acquisitions per `windowMs`. */
export function createLimiter(max: number, windowMs: number, now: () => number = Date.now) {
  const hits: number[] = [];
  return {
    acquire(): void {
      const t = now();
      while (hits.length && hits[0]! <= t - windowMs) hits.shift();
      if (hits.length >= max) throw new RateLimited(Math.max(1, Math.ceil((hits[0]! + windowMs - t) / 1000)));
      hits.push(t);
    },
  };
}

/** Public-only view: wikis never see private layers, whoever asks. */
export async function publicView(db: Db): Promise<View> {
  return { visible: await db.publicLayers(), writable: [] };
}

/** Newest change seq touching the entity group (subject or object), visible under the view. */
export async function entitySeq(tx: Tx, group: string[]): Promise<number> {
  const [row] = await tx<{ seq: string | null }[]>`
    select greatest(
      (select max(ch.seq) from change ch
        where ch.subject = any(${group}::text[]) and (ch.layer_id is null or ch.layer_id = any(rebox_visible()))),
      (select max(ch.seq) from change ch
        where ch.object_id = any(${group}::text[]) and (ch.layer_id is null or ch.layer_id = any(rebox_visible())))
    )::text as seq`;
  return Number(row?.seq ?? 0);
}

function toWiki(r: WikiRow, currentSeq: number): Wiki {
  const input = Number(r.input_seq);
  return {
    entity_id: r.entity_id,
    content_md: r.content_md,
    model: r.model,
    generated_at: r.generated_at.toISOString(),
    input_seq: input,
    stale: currentSeq > input,
    sources: r.sources ?? [],
  };
}

export type Resolved = { entity: EntityRef; group: string[] } | null;

/** Canonical, publicly visible entity for `id`, with its merge group. */
export async function resolvePublic(tx: Tx, id: string): Promise<Resolved> {
  const canon = (await canonicalIds(tx, [id])).get(id);
  if (!canon) return null;
  const ref = (await entityRefs(tx, [canon])).get(canon);
  if (!ref) return null;
  return { entity: ref, group: [canon, ...(await mergedFrom(tx, canon))] };
}

export interface WikiService {
  get(id: string): Promise<Wiki | null | 'missing'>;
  /** Generates unless a fresh article exists (or `force`). Throws RateLimited. */
  generate(id: string, opts?: { force?: boolean; by?: string }): Promise<Wiki | 'missing'>;
}

export function createWikiService(deps: {
  db: Db;
  vocab: { get(): Promise<Vocab> };
  model: WikiModel;
  limiter: { acquire(): void };
  log?: (line: Record<string, unknown>) => void;
}): WikiService {
  const { db, model, limiter } = deps;
  const inflight = new Map<string, Promise<Wiki | 'missing'>>();

  async function read(tx: Tx, entityId: string, group: string[]): Promise<Wiki | null> {
    const [row] = await tx<WikiRow[]>`
      select entity_id, content_md, model, input_seq::text as input_seq, sources, generated_at
      from entity_wiki where entity_id = ${entityId}`;
    if (!row) return null;
    return toWiki(row, await entitySeq(tx, group));
  }

  async function get(id: string): Promise<Wiki | null | 'missing'> {
    return db.withView(await publicView(db), async (tx) => {
      const r = await resolvePublic(tx, id);
      if (!r) return 'missing';
      return read(tx, r.entity.id, r.group);
    });
  }

  async function run(id: string, force: boolean, by: string): Promise<Wiki | 'missing'> {
    const view = await publicView(db);
    const vocab = await deps.vocab.get();
    const prep = await db.withView(view, async (tx) => {
      const r = await resolvePublic(tx, id);
      if (!r) return 'missing' as const;
      const existing = await read(tx, r.entity.id, r.group);
      if (existing && !existing.stale && !force) return { done: existing, input: null };
      const input = await gatherWikiInput(tx, r.entity, vocab);
      // Staleness baseline = the entity's own change horizon at read time.
      input.input_seq = await entitySeq(tx, r.group);
      return { done: null, input };
    });
    if (prep === 'missing') return 'missing';
    if (prep.done) return prep.done;
    if (!prep.input) return 'missing';

    limiter.acquire();
    const started = performance.now();
    const out = await model.complete(wikiPrompt(prep.input));
    const input = prep.input;
    const { content_md, cited } = finishArticle(out.text, input.sources);
    deps.log?.({
      at: new Date().toISOString(),
      level: 'info',
      msg: 'wiki generated',
      entity: input.entity.id,
      model: out.model,
      input_tokens: out.input_tokens,
      output_tokens: out.output_tokens,
      stop_reason: out.stop_reason,
      ms: Math.round(performance.now() - started),
    });

    return db.withView(view, async (tx) => {
      const [row] = await tx<WikiRow[]>`
        insert into entity_wiki (entity_id, content_md, model, input_seq, sources, generated_at, generated_by)
        values (${input.entity.id}, ${content_md}, ${out.model || model.id}, ${input.input_seq},
                ${tx.json(cited as never)}, now(), ${by})
        on conflict (entity_id) do update set content_md = excluded.content_md, model = excluded.model,
          input_seq = excluded.input_seq, sources = excluded.sources, generated_at = excluded.generated_at,
          generated_by = excluded.generated_by
        returning entity_id, content_md, model, input_seq::text as input_seq, sources, generated_at`;
      const group = [input.entity.id, ...(await mergedFrom(tx, input.entity.id))];
      return toWiki(row!, await entitySeq(tx, group));
    });
  }

  return {
    get,
    generate(id, opts = {}) {
      const key = id;
      const cur = inflight.get(key);
      if (cur) return cur;
      const p = run(id, opts.force ?? false, opts.by ?? 'api').finally(() => inflight.delete(key));
      inflight.set(key, p);
      return p;
    },
  };
}
