import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import type { Config } from './config.js';
import { NO_VIEW, type Db, type Tx, type View } from './db.js';
import { ApiError, badRequest } from './errors.js';

export type Role = 'anonymous' | 'ingest' | 'admin' | 'worker';

export interface Principal {
  role: Role;
  /** Dev-only viewer from X-Rebox-Viewer; adds that user's and their teams' layers. */
  viewer: string | null;
  /** role 'worker': the external worker this token belongs to (CONTRACTS §12). */
  workerId?: string;
}

/** External worker tokens: `rbxw_<worker id>_<32 hex>` (CONTRACTS §12). */
const WORKER_TOKEN_RE = /^rbxw_([a-z0-9][a-z0-9-]{2,62})_([0-9a-f]{32})$/;

export function sha256Hex(s: string): string {
  return createHash('sha256').update(s).digest('hex');
}

/** A new external worker token and the hash to store (the token itself is never stored). */
export function newWorkerToken(workerId: string): { token: string; hash: string } {
  const token = `rbxw_${workerId}_${randomBytes(16).toString('hex')}`;
  return { token, hash: sha256Hex(token) };
}

const VIEWER_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;

function digest(s: string): Buffer {
  return createHash('sha256').update(s).digest();
}

/** Constant-time string comparison (hashing first makes lengths equal). */
export function tokenEquals(given: string, expected: string): boolean {
  return timingSafeEqual(digest(given), digest(expected));
}

const unauthorized = () => new ApiError(401, 'unauthorized', 'invalid or missing credentials');
export const forbidden = (message = 'forbidden') => new ApiError(403, 'forbidden', message);

/**
 * An external worker token is valid when its worker exists, is not revoked, and the stored
 * sha256 matches (compared in constant time). Paused workers authenticate; the writer then
 * rejects their writes as not active.
 */
async function verifyWorkerToken(db: Db, token: string): Promise<string | null> {
  const m = WORKER_TOKEN_RE.exec(token);
  if (!m) return null;
  const id = m[1]!;
  const [row] = await db.withView(NO_VIEW, (tx) =>
    tx<{ token_hash: string | null; status: string }[]>`select token_hash, status from worker where id = ${id}`,
  );
  // Hash and compare even when the row is missing, so timing does not reveal which ids exist.
  const expected = Buffer.from(row?.token_hash ?? sha256Hex(`missing:${id}`), 'hex');
  const given = createHash('sha256').update(token).digest();
  const ok = expected.length === given.length && timingSafeEqual(expected, given);
  if (!row || !row.token_hash || !ok || row.status === 'revoked') return null;
  return id;
}

/**
 * Resolves the caller from headers. A Bearer token that matches no configured token (nor a live
 * external worker token) is a 401, never a silent downgrade to anonymous.
 */
export async function authenticate(
  config: Config,
  headers: { authorization?: string | undefined; viewer?: string | undefined },
  db?: Db,
): Promise<Principal> {
  let role: Role = 'anonymous';
  let workerId: string | undefined;
  const auth = headers.authorization?.trim();
  if (auth) {
    const m = /^Bearer\s+(\S+)$/i.exec(auth);
    if (!m) throw unauthorized();
    const token = m[1]!;
    if (config.adminToken && tokenEquals(token, config.adminToken)) role = 'admin';
    else if (config.ingestToken && tokenEquals(token, config.ingestToken)) role = 'ingest';
    else if (db && token.startsWith('rbxw_')) {
      const id = await verifyWorkerToken(db, token);
      if (!id) throw unauthorized();
      role = 'worker';
      workerId = id;
    } else throw unauthorized();
  }

  let viewer: string | null = null;
  const rawViewer = headers.viewer?.trim();
  if (rawViewer && config.env === 'dev') {
    if (!VIEWER_RE.test(rawViewer)) throw badRequest('X-Rebox-Viewer must match [a-z0-9][a-z0-9_.-]*');
    viewer = rawViewer;
  }
  return workerId ? { role, viewer, workerId } : { role, viewer };
}

export function requireRole(p: Principal, role: Exclude<Role, 'anonymous'>, ...more: Exclude<Role, 'anonymous'>[]): void {
  if (p.role === 'anonymous') throw unauthorized();
  if (p.role !== role && !more.includes(p.role)) throw forbidden(`requires the ${role} token`);
}

/** user/<viewer>/* layers plus team/<t>/* for every team the viewer belongs to. */
async function viewerLayers(db: Db, viewer: string): Promise<string[]> {
  return db.withView({ visible: [], writable: [] }, async (tx: Tx) => {
    const rows = await tx<{ id: string }[]>`
      select id from layer
      where (scope = 'user' and owner = ${viewer})
         or (scope = 'team' and owner in (select team_id from team_member where user_id = ${viewer}))
      order by id`;
    return rows.map((r) => r.id);
  });
}

/** CONTRACTS §2: every read sees all public layers, plus the dev viewer's private layers. */
export async function readView(db: Db, p: Principal): Promise<View> {
  const pub = await db.publicLayers();
  const extra = p.viewer ? await viewerLayers(db, p.viewer) : [];
  return { visible: [...pub, ...extra], writable: [] };
}

/** Admin (merge/split): sees and writes all public layers. */
export async function adminView(db: Db): Promise<View> {
  const pub = await db.publicLayers();
  return { visible: pub, writable: pub };
}
