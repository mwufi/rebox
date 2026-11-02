import { badRequest } from './errors.js';

/** Opaque cursor: base64url(JSON of the last sort key). */
export function encodeCursor(key: unknown): string {
  return Buffer.from(JSON.stringify(key), 'utf8').toString('base64url');
}

export function decodeCursor<T>(cursor: string | null | undefined, guard: (v: unknown) => v is T): T | null {
  if (cursor === null || cursor === undefined || cursor === '') return null;
  try {
    const value: unknown = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'));
    if (guard(value)) return value;
  } catch {
    // fall through
  }
  throw badRequest('invalid cursor');
}

export const isIdCursor = (v: unknown): v is { id: string } =>
  typeof v === 'object' && v !== null && typeof (v as { id?: unknown }).id === 'string';
