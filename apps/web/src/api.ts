/**
 * Typed client for the Rebox read API (CONTRACTS §6) and admin API (§7), written by hand from the
 * contract. Same-origin by default: the API serves this app.
 */

export type Kind = 'person' | 'company' | 'launch' | 'event' | 'place' | (string & {});

export interface EntityRef {
  id: string;
  kind: Kind;
  name: string | null;
  /** S10, optional: folded avatar URL. */
  avatar?: string;
  /** S10, optional: the name is only an HN / X handle. */
  handle?: 'hn' | 'x';
}

export interface Fact {
  predicate: string;
  value: unknown;
  object: EntityRef | null;
  subject: EntityRef;
  valid_from: string | null;
  valid_to: string | null;
  layer: string;
  source: string;
  source_ref: string;
  updated_at: string;
  /** CONTRACTS §12 (S7): present once the API serves provenance; render defensively. */
  worker?: string | null;
  evidence?: Evidence[] | null;
  support?: number | null;
}

export interface Evidence {
  url: string;
  author?: string | null;
  quote?: string | null;
  observed_at?: string | null;
}

export type TimelineKind =
  | 'founded'
  | 'joined'
  | 'left'
  | 'started'
  | 'ended'
  | 'launched'
  | 'posted'
  | 'attended'
  | 'acquired'
  | 'invested'
  | 'milestone'
  | 'first_seen'
  | (string & {});

/** GET /v0/entities/:id/timeline (§14). Fields beyond the contract are optional. */
export interface TimelineEvent {
  at: string;
  kind: TimelineKind;
  title: string;
  predicate: string | null;
  other: EntityRef | null;
  fact: Fact | null;
  key?: string;
  dir?: 'out' | 'in' | null;
  precision?: 'year' | 'day' | 'time';
  headline?: boolean;
  others?: EntityRef[];
  layer?: string;
}

export interface Timeline {
  id?: string;
  events: TimelineEvent[];
  next?: string | null;
}

/** GET/POST /v0/entities/:id/wiki (§14). */
export interface Wiki {
  content_md: string;
  model: string;
  generated_at: string;
  input_seq: number;
  stale: boolean;
  sources?: { n: number; url: string; label: string }[];
  can_generate?: boolean;
  public?: boolean;
}

export interface EntityView {
  id: string;
  kind: Kind;
  name: string | null;
  merged_from: string[];
  facts: Fact[];
  incoming: Fact[];
}

/** GET /v0/entities/:id — either the view, or a redirect plus the target's view. */
export type EntityResponse = EntityView | { redirect: string; entity: EntityView };

export interface Stats {
  entities: Record<string, number>;
  claims: number;
  layers: { id: string; claims: number }[];
  last_seq: number;
}

export interface Predicate {
  id: string;
  subject_kinds: string[];
  object_kind: string | null;
  cardinality: 'one' | 'many';
  meta: {
    label?: string;
    value_type?: 'text' | 'url' | 'date' | 'int' | 'number' | 'bool';
    identity?: boolean;
    normalize?: string;
    filterable?: boolean;
    private?: boolean;
    props?: Record<string, string>;
  };
}

export interface SearchHit extends EntityRef {
  score: number;
}

export interface Change {
  seq: number;
  op: 'insert' | 'update' | 'delete' | 'merge' | 'split';
  layer_id: string | null;
  subject: string | null;
  predicate: string | null;
  object_id: string | null;
  at: string;
}

export type Filter =
  | { name: string }
  | {
      edge: {
        predicate: string;
        dir?: 'out' | 'in';
        to?: string | null;
        during?: { from?: string | null; to?: string | null } | null;
      };
    }
  | { attr: { predicate: string; eq?: string | number | boolean; gte?: string | number; lte?: string | number; contains?: string } };

export interface Query {
  find: Kind;
  where: Filter[];
  limit?: number;
  cursor?: string | null;
}

export interface QueryResult {
  results: (EntityRef & { reasons: string[] })[];
  next: string | null;
}

export interface ReviewItem {
  id: number;
  kind: string;
  entity_ids: string[];
  reason: string;
  evidence: unknown;
  status: 'open' | 'merged' | 'dismissed';
  resolved_by: string | null;
  created_at: string;
  resolved_at: string | null;
  candidates: EntityRef[];
}

export interface ErrorBody {
  error: string;
  message?: string;
  can_generate?: boolean;
  public?: boolean;
  retry_after?: number;
  details?: { index: number; message: string }[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: ErrorBody | null,
  ) {
    super(
      body?.details?.map((d) => d.message).join('; ') ?? body?.message ?? body?.error ?? `request failed (${status})`,
    );
  }
}

export interface ClientOptions {
  base?: string;
  fetch?: typeof fetch;
}

const enc = encodeURIComponent;

function qs(params: Record<string, string | number | null | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${enc(k)}=${enc(String(v))}`);
  return parts.length ? `?${parts.join('&')}` : '';
}

export function createClient(opts: ClientOptions = {}) {
  const base = (opts.base ?? '').replace(/\/$/, '');
  const f = opts.fetch ?? ((...args: Parameters<typeof fetch>) => fetch(...args));

  async function call<T>(method: 'GET' | 'POST', path: string, init: { body?: unknown; token?: string; signal?: AbortSignal } = {}): Promise<T> {
    const headers: Record<string, string> = { accept: 'application/json' };
    if (init.body !== undefined) headers['content-type'] = 'application/json';
    if (init.token) headers.authorization = `Bearer ${init.token}`;
    const res = await f(`${base}${path}`, {
      method,
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init.signal,
    });
    const text = await res.text();
    let json: unknown = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch {
      json = null;
    }
    if (!res.ok) throw new ApiError(res.status, json as ErrorBody | null);
    return json as T;
  }

  type Sig = { signal?: AbortSignal };

  return {
    health: (o: Sig = {}) => call<{ ok: boolean; db: boolean }>('GET', '/health', o),
    stats: (o: Sig = {}) => call<Stats>('GET', '/v0/stats', o),
    predicates: (o: Sig = {}) => call<Predicate[]>('GET', '/v0/predicates', o),
    search: (q: string, p: { kind?: string; limit?: number } & Sig = {}) =>
      call<SearchHit[]>('GET', `/v0/search${qs({ q, kind: p.kind, limit: p.limit })}`, p),
    entity: (id: string, o: Sig = {}) => call<EntityResponse>('GET', `/v0/entities/${enc(id)}`, o),
    entities: (ids: string[], o: Sig = {}) =>
      ids.length ? call<EntityRef[]>('GET', `/v0/entities${qs({ ids: ids.join(',') })}`, o) : Promise.resolve([]),
    edges: (id: string, p: { predicate?: string; dir?: 'out' | 'in'; limit?: number; cursor?: string | null } & Sig = {}) =>
      call<{ edges: Fact[]; next: string | null }>(
        'GET',
        `/v0/entities/${enc(id)}/edges${qs({ predicate: p.predicate, dir: p.dir, limit: p.limit, cursor: p.cursor })}`,
        p,
      ),
    timeline: (id: string, p: { limit?: number; cursor?: string | null } & Sig = {}) =>
      call<Timeline>('GET', `/v0/entities/${enc(id)}/timeline${qs({ limit: p.limit, cursor: p.cursor })}`, p),
    wiki: (id: string, o: Sig = {}) => call<Wiki>('GET', `/v0/entities/${enc(id)}/wiki`, o),
    generateWiki: (id: string, p: { token?: string; force?: boolean } = {}) =>
      call<Wiki>('POST', `/v0/entities/${enc(id)}/wiki${qs({ force: p.force ? 'true' : undefined })}`, { token: p.token || undefined, body: {} }),
    query: (q: Query, o: Sig = {}) => call<QueryResult>('POST', '/v0/query', { body: q, ...o }),
    changes: (p: { after?: number; limit?: number } & Sig = {}) =>
      call<{ changes: Change[]; next: number }>('GET', `/v0/changes${qs({ after: p.after, limit: p.limit })}`, p),

    // ── Admin (§7) ──
    review: (token: string, status: ReviewItem['status'] = 'open', o: Sig = {}) =>
      call<ReviewItem[]>('GET', `/v0/review${qs({ status })}`, { token, ...o }),
    merge: (token: string, body: { from: string; into: string; review_id?: number | null }) =>
      call<{ merged: { from: string; into: string }; review_id: number | null; seq: number }>('POST', '/v0/admin/merge', { token, body }),
    split: (token: string, id: string) =>
      call<{ split: { id: string; from: string }; seq: number }>('POST', '/v0/admin/split', { token, body: { id } }),
    dismiss: (token: string, reviewId: number) =>
      call<ReviewItem>('POST', `/v0/admin/review/${enc(String(reviewId))}/dismiss`, { token, body: {} }),
  };
}

export type Client = ReturnType<typeof createClient>;

export const isRedirect = (r: EntityResponse): r is { redirect: string; entity: EntityView } => 'redirect' in r;

export const api: Client = createClient();
