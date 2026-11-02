/** Strict ISO 8601 date / timestamp check (rejects 2021-02-30, 2021-13-01, 25:00, ...). */
const ISO_RE =
  /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$/;

export function isIsoDate(s: string): boolean {
  const m = ISO_RE.exec(s);
  if (!m) return false;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const day = new Date(Date.UTC(y, mo - 1, d));
  if (day.getUTCFullYear() !== y || day.getUTCMonth() !== mo - 1 || day.getUTCDate() !== d) return false;
  if (m[4] !== undefined && (Number(m[4]) > 23 || Number(m[5]) > 59 || Number(m[6] ?? 0) > 59)) return false;
  return true;
}

/** Milliseconds since epoch; date-only strings are UTC midnight (the DB session runs in UTC). */
export function isoToMs(s: string): number {
  return Date.parse(s.length === 10 ? `${s}T00:00:00Z` : s.replace(' ', 'T'));
}
