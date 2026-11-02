/**
 * Claims writer (CONTRACTS §5): the only write path. Validates the whole batch first, then mints
 * entities and upserts claims in a single transaction scoped to the request layer.
 */
import { z } from 'zod';
import type { Db, Tx } from './db.js';
import { isIsoDate, isoToMs } from './dates.js';
import { ApiError, validation, type ErrorDetail } from './errors.js';
import { checkNormalized, isNormalizerName } from './normalize.js';
import { isEdge, type Predicate, type Vocab, type VocabCache } from './vocab.js';

export const MAX_BATCH = 1000;

const ENTITY_ID_RE = /^(rbx|local):([a-z]+):[a-z0-9-]+$/;
const LAYER_RE = /^(?:(public|firm)\/([a-z0-9_-]+)|(team|user)\/([a-z0-9_.-]+)\/([a-z0-9_-]+))$/;

const entityId = z.string().regex(ENTITY_ID_RE, 'must match (rbx|local):<kind>:<[a-z0-9-]+>');
const refSchema = z.union([
  z.object({ id: entityId }).strict(),
  z.object({ mint: z.object({ id: entityId, kind: z.string().regex(/^[a-z]+$/) }).strict() }).strict(),
]);
const isoDate = z.string().refine(isIsoDate, 'must be a valid ISO date or timestamp');

const claimSchema = z
  .object({
    subject: refSchema,
    predicate: z.string().min(1),
    object: refSchema.nullish(),
    value: z.unknown().optional(),
    valid_from: isoDate.nullish(),
    valid_to: isoDate.nullish(),
    retract: z.boolean().optional(),
    source_ref: z.string().min(1).max(1000),
  })
  .strict();

const bodySchema = z
  .object({
    layer: z.string().regex(LAYER_RE, 'must be public|firm/<source> or team|user/<owner>/<source>'),
    claims: z.array(z.unknown()).min(1).max(MAX_BATCH),
  })
  .strict();

type Ref = z.infer<typeof refSchema>;
type ClaimIn = z.infer<typeof claimSchema>;

export interface WriteResult {
  written: number;
  minted: number;
}

export interface ParsedLayer {
  id: string;
  scope: 'public' | 'firm' | 'team' | 'user';
  owner: string | null;
  source: string;
}

export function parseLayer(id: string): ParsedLayer | null {
  const m = LAYER_RE.exec(id);
  if (!m) return null;
  if (m[1]) return { id, scope: m[1] as 'public' | 'firm', owner: null, source: m[2]! };
  return { id, scope: m[3] as 'team' | 'user', owner: m[4]!, source: m[5]! };
}

const refId = (r: Ref): string => ('id' in r ? r.id : r.mint.id);
const kindFromId = (id: string): string => ENTITY_ID_RE.exec(id)![2]!;

function zodMessage(err: z.ZodError, prefix = ''): string {
  return err.issues
    .map((i) => {
      const path = [prefix, ...i.path.map(String)].filter(Boolean).join('.');
      return path ? `${path}: ${i.message}` : i.message;
    })
    .join('; ');
}

function isScalar(v: unknown): boolean {
  return v === null || ['string', 'number', 'boolean'].includes(typeof v);
}

function checkValueType(p: Predicate, value: unknown): string | null {
  if (value === null || value === undefined) return 'attribute claims need a non-null value';
  if (Array.isArray(value)) {
    return value.every(isScalar) ? null : 'attribute value must be a JSON scalar or an array of scalars';
  }
  if (!isScalar(value)) return 'attribute value must be a JSON scalar or an array of scalars';
  switch (p.meta.value_type) {
    case 'int':
      return Number.isInteger(value) ? null : 'value must be an integer';
    case 'number':
      return typeof value === 'number' && Number.isFinite(value) ? null : 'value must be a number';
    case 'bool':
      return typeof value === 'boolean' ? null : 'value must be a boolean';
    case 'text':
    case 'url':
    case 'date':
      return typeof value === 'string' ? null : 'value must be a string';
    default:
      return null;
  }
}

/** Validates one claim's shape against the vocabulary. Entity existence is checked later in SQL. */
function validateClaim(c: ClaimIn, vocab: Vocab, layer: ParsedLayer): string[] {
  const errors: string[] = [];
  const p = vocab.predicates.get(c.predicate);
  if (!p) return [`unknown predicate ${JSON.stringify(c.predicate)}`];

  if ('mint' in c.subject && kindFromId(c.subject.mint.id) !== c.subject.mint.kind) {
    errors.push(`subject.mint: id kind does not match kind ${JSON.stringify(c.subject.mint.kind)}`);
  }
  if (c.object && 'mint' in c.object && kindFromId(c.object.mint.id) !== c.object.mint.kind) {
    errors.push(`object.mint: id kind does not match kind ${JSON.stringify(c.object.mint.kind)}`);
  }
  // Kinds are encoded in ids, so subject/object kind checks need no DB lookup.
  const subjectKind = kindFromId(refId(c.subject));
  if (!p.subject_kinds.includes(subjectKind)) {
    errors.push(`subject kind ${subjectKind} not allowed for ${p.id} (expected ${p.subject_kinds.join('|')})`);
  }

  if (isEdge(p)) {
    if (!c.object) errors.push(`${p.id} is an edge: object is required`);
    else {
      const objectKind = kindFromId(refId(c.object));
      if (objectKind !== p.object_kind) errors.push(`object kind ${objectKind} must be ${p.object_kind} for ${p.id}`);
    }
    const v = c.value;
    if (v !== undefined && v !== null && (typeof v !== 'object' || Array.isArray(v))) {
      errors.push('edge value must be an object of edge props or null');
    }
  } else {
    if (c.object) errors.push(`${p.id} is an attribute: object is not allowed`);
    // Retracting a `one` attribute hides whatever value wins, so the retract needs no value.
    const valuelessRetract = c.retract === true && p.cardinality === 'one' && (c.value === null || c.value === undefined);
    const typeErr = valuelessRetract ? null : checkValueType(p, c.value);
    if (typeErr) errors.push(typeErr);
    else if (!valuelessRetract && p.meta.identity && isNormalizerName(p.meta.normalize)) {
      const values = Array.isArray(c.value) ? c.value : [c.value];
      for (const v of values) {
        const err = checkNormalized(p.meta.normalize, v);
        if (err) errors.push(`identity value ${JSON.stringify(v)} ${err}`);
      }
    }
  }

  if (p.meta.private && layer.scope === 'public') {
    errors.push(`${p.id} is private and cannot be written to a public layer`);
  }
  if (c.valid_from && c.valid_to && isoToMs(c.valid_from) > isoToMs(c.valid_to)) {
    errors.push('valid_from must not be after valid_to');
  }
  return errors;
}

function parseBody(body: unknown): { layer: ParsedLayer; claims: ClaimIn[] } {
  const parsed = bodySchema.safeParse(body);
  if (!parsed.success) throw validation([{ index: -1, message: zodMessage(parsed.error) }]);
  const layer = parseLayer(parsed.data.layer)!;
  const details: ErrorDetail[] = [];
  const claims: ClaimIn[] = [];
  parsed.data.claims.forEach((raw, index) => {
    const r = claimSchema.safeParse(raw);
    if (!r.success) details.push({ index, message: zodMessage(r.error) });
    else claims.push(r.data);
  });
  if (details.length) throw validation(details);
  return { layer, claims };
}

interface MintRequest {
  id: string;
  kind: string;
  firstIndex: number;
}

/** Collects mints, rejecting the same id minted with two kinds in one batch. */
function collectMints(claims: ClaimIn[]): { mints: Map<string, MintRequest>; details: ErrorDetail[] } {
  const mints = new Map<string, MintRequest>();
  const details: ErrorDetail[] = [];
  claims.forEach((c, index) => {
    for (const ref of [c.subject, c.object]) {
      if (!ref || !('mint' in ref)) continue;
      const prev = mints.get(ref.mint.id);
      if (!prev) mints.set(ref.mint.id, { ...ref.mint, firstIndex: index });
      else if (prev.kind !== ref.mint.kind) {
        details.push({ index, message: `${ref.mint.id} minted with conflicting kinds` });
      }
    }
  });
  return { mints, details };
}

async function ensureLayer(tx: Tx, layer: ParsedLayer): Promise<boolean> {
  const [existing] = await tx<{ scope: string }[]>`select scope::text from layer where id = ${layer.id}`;
  if (existing) return false;
  if (layer.scope !== 'public') {
    throw validation([{ index: -1, message: `layer ${layer.id} does not exist (only public/* layers are auto-created)` }]);
  }
  await tx`
    insert into layer (id, scope, owner, source) values (${layer.id}, 'public', null, ${layer.source})
    on conflict (id) do nothing`;
  return true;
}

/**
 * Inserts minted entities that don't exist yet. An existing id is reused, not an error; whether it
 * is visible and of the right kind is checked afterwards by checkReferences. Returns the number of
 * new entities.
 */
async function mintEntities(tx: Tx, layer: string, mints: Map<string, MintRequest>): Promise<number> {
  if (mints.size === 0) return 0;
  const ids = [...mints.keys()];
  const kinds = ids.map((id) => mints.get(id)!.kind);
  const inserted = await tx<{ id: string }[]>`
    insert into entity (id, kind, home_layer)
    select id, kind, ${layer} from unnest(${ids}::text[], ${kinds}::text[]) as m(id, kind)
    on conflict (id) do nothing
    returning id`;
  return inserted.length;
}

/** Every referenced id must be visible to the writer now (after minting). */
async function checkReferences(tx: Tx, claims: ClaimIn[]): Promise<void> {
  const ids = new Set<string>();
  for (const c of claims) {
    ids.add(refId(c.subject));
    if (c.object) ids.add(refId(c.object));
  }
  const rows = await tx<{ id: string; kind: string }[]>`select id, kind from entity where id = any(${[...ids]}::text[])`;
  const kindOf = new Map(rows.map((r) => [r.id, r.kind]));
  const details: ErrorDetail[] = [];
  claims.forEach((c, index) => {
    for (const [side, ref] of [['subject', c.subject], ['object', c.object]] as const) {
      if (!ref) continue;
      const id = refId(ref);
      const kind = kindOf.get(id);
      if (kind === undefined) details.push({ index, message: `${side} ${id} does not exist or is not visible` });
      else if (kind !== kindFromId(id)) details.push({ index, message: `${side} ${id} exists with kind ${kind}` });
    }
  });
  if (details.length) throw validation(details);
}

/**
 * Upserts claims on the claim_identity_uq key. Duplicate keys within one batch keep the last
 * occurrence (latest write wins) since ON CONFLICT cannot touch a row twice in one statement.
 * Re-sending an unchanged claim is a no-op: no updated_at bump (which would reorder the fold's
 * "newest wins" tie-break) and no `change` row. Returns the number of rows inserted or changed.
 */
async function upsertClaims(tx: Tx, layer: string, claims: ClaimIn[]): Promise<number> {
  const byKey = new Map<string, Record<string, unknown>>();
  for (const c of claims) {
    const subject = refId(c.subject);
    const object = c.object ? refId(c.object) : null;
    const key = JSON.stringify([c.source_ref, subject, c.predicate, object ?? '']);
    byKey.delete(key); // keep insertion order = last occurrence
    byKey.set(key, {
      subject,
      predicate: c.predicate,
      object_id: object,
      value: c.value === undefined ? null : c.value,
      valid_from: c.valid_from ?? null,
      valid_to: c.valid_to ?? null,
      retract: c.retract ?? false,
      source_ref: c.source_ref,
    });
  }
  const rows = [...byKey.values()];
  const result = await tx`
    insert into claim (layer_id, subject, predicate, object_id, value, valid_from, valid_to, retract, source_ref)
    select ${layer}, x.subject, x.predicate, x.object_id, x.value, x.valid_from, x.valid_to,
           coalesce(x.retract, false), x.source_ref
    from jsonb_to_recordset(${tx.json(rows as never)}::jsonb) as x(
      subject text, predicate text, object_id text, value jsonb,
      valid_from timestamptz, valid_to timestamptz, retract boolean, source_ref text)
    on conflict (layer_id, source_ref, subject, predicate, coalesce(object_id, ''))
    do update set value = excluded.value, valid_from = excluded.valid_from, valid_to = excluded.valid_to,
                  retract = excluded.retract, updated_at = now()
    where (claim.value, claim.valid_from, claim.valid_to, claim.retract)
          is distinct from (excluded.value, excluded.valid_from, excluded.valid_to, excluded.retract)`;
  return result.count;
}

export interface Writer {
  write(body: unknown): Promise<WriteResult>;
}

export function createWriter(db: Db, vocabCache: VocabCache): Writer {
  return {
    async write(body) {
      const { layer, claims } = parseBody(body);
      const vocab = await vocabCache.get();

      const details: ErrorDetail[] = [];
      claims.forEach((c, index) => {
        for (const message of validateClaim(c, vocab, layer)) details.push({ index, message });
      });
      const { mints, details: mintDetails } = collectMints(claims);
      details.push(...mintDetails);
      if (details.length) throw validation(details.sort((a, b) => a.index - b.index));

      // CONTRACTS §2: the ingest token sees all public layers (read fresh, inside the same
      // statement) plus the target layer, and writes only the target layer.
      const view = { visible: [layer.id], writable: [layer.id], allPublic: true };
      try {
        const result = await db.withView(view, async (tx) => {
          const created = await ensureLayer(tx, layer);
          const minted = await mintEntities(tx, layer.id, mints);
          await checkReferences(tx, claims);
          const written = await upsertClaims(tx, layer.id, claims);
          return { written, minted, created };
        });
        if (result.created) db.invalidatePublicLayers();
        return { written: result.written, minted: result.minted };
      } catch (err) {
        if (err instanceof ApiError) throw err;
        throw mapDbError(err);
      }
    },
  };
}

/** Maps constraint violations that slip past validation (e.g. races) to a 400, never a 500. */
function mapDbError(err: unknown): unknown {
  const e = err as { code?: string; message?: string };
  if (e.code === '23503' || e.code === '23514' || e.code === '42501' || e.code === '22007' || e.code === '22008') {
    return validation([{ index: -1, message: `rejected by database: ${e.message ?? e.code}` }]);
  }
  return err;
}
