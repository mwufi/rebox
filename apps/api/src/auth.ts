import { createHash, timingSafeEqual } from 'node:crypto';
import type { Config } from './config.js';
import type { Db, Tx, View } from './db.js';
import { ApiError, badRequest } from './errors.js';

export type Role = 'anonymous' | 'ingest' | 'admin';

export interface Principal {
  role: Role;
  /** Dev-only viewer from X-Rebox-Viewer; adds that user's and their teams' layers. */
  viewer: string | null;
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
 * Resolves the caller from headers. A Bearer token that matches no configured token is a 401,
 * never a silent downgrade to anonymous.
 */
export function authenticate(
  config: Config,
  headers: { authorization?: string | undefined; viewer?: string | undefined },
): Principal {
  let role: Role = 'anonymous';
  const auth = headers.authorization?.trim();
  if (auth) {
    const m = /^Bearer\s+(\S+)$/i.exec(auth);
    if (!m) throw unauthorized();
    const token = m[1]!;
    if (config.adminToken && tokenEquals(token, config.adminToken)) role = 'admin';
    else if (config.ingestToken && tokenEquals(token, config.ingestToken)) role = 'ingest';
    else throw unauthorized();
  }

  let viewer: string | null = null;
  const rawViewer = headers.viewer?.trim();
  if (rawViewer && config.env === 'dev') {
    if (!VIEWER_RE.test(rawViewer)) throw badRequest('X-Rebox-Viewer must match [a-z0-9][a-z0-9_.-]*');
    viewer = rawViewer;
  }
  return { role, viewer };
}

export function requireRole(p: Principal, role: Exclude<Role, 'anonymous'>): void {
  if (p.role === 'anonymous') throw unauthorized();
  if (p.role !== role) throw forbidden(`requires the ${role} token`);
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

/** Ingest: sees public layers and the target layer; may write only the target layer. */
export async function writerView(db: Db, layer: string): Promise<View> {
  const pub = await db.publicLayers();
  return { visible: pub.includes(layer) ? pub : [...pub, layer], writable: [layer] };
}

/** Admin (merge/split): sees and writes all public layers. */
export async function adminView(db: Db): Promise<View> {
  const pub = await db.publicLayers();
  return { visible: pub, writable: pub };
}
