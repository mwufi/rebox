/** CONTRACTS §4 normalizers. The writer only checks that identity values are already normalized. */
export type NormalizerName = 'qid' | 'handle' | 'domain' | 'url' | 'email' | 'exact';

const SCHEME_RE = /^([a-z][a-z0-9+.-]*):\/\//i;

function stripWww(host: string): string {
  return host.startsWith('www.') ? host.slice(4) : host;
}

export const normalizers: Record<NormalizerName, (raw: string) => string | null> = {
  qid: (raw) => {
    const v = raw.trim().toUpperCase();
    return /^Q\d+$/.test(v) ? v : null;
  },
  handle: (raw) => {
    let v = raw.trim().toLowerCase();
    if (SCHEME_RE.test(v) || v.includes('/')) {
      // https://x.com/jack, x.com/jack, github.com/jack/ -> first path segment
      const rest = v.replace(SCHEME_RE, '');
      const segment = rest.split(/[?#]/, 1)[0]!.split('/').filter(Boolean)[1];
      if (!segment) return null;
      v = segment;
    }
    v = v.replace(/^@+/, '');
    return v.length > 0 ? v : null;
  },
  domain: (raw) => {
    const rest = raw.trim().toLowerCase().replace(SCHEME_RE, '');
    const host = rest.split(/[/?#]/, 1)[0]!.replace(/:\d*$/, '');
    const v = stripWww(host);
    return v.length > 0 ? v : null;
  },
  url: (raw) => {
    const m = /^([a-z][a-z0-9+.-]*):\/\/([^/?#]+)([^?#]*)/i.exec(raw.trim());
    if (!m) return null;
    const scheme = m[1]!.toLowerCase();
    const host = stripWww(m[2]!.toLowerCase());
    const path = m[3]!.replace(/\/+$/, '');
    return `${scheme}://${host}${path}`;
  },
  email: (raw) => {
    const v = raw.trim().toLowerCase();
    return v.length > 0 ? v : null;
  },
  exact: (raw) => {
    const v = raw.trim();
    return v.length > 0 ? v : null;
  },
};

export function isNormalizerName(name: unknown): name is NormalizerName {
  return typeof name === 'string' && Object.hasOwn(normalizers, name);
}

/** Returns null when the value is normalized, else a message naming the expected form. */
export function checkNormalized(name: NormalizerName, value: unknown): string | null {
  if (typeof value !== 'string') return `must be a string (normalizer ${name})`;
  const normalized = normalizers[name](value);
  if (normalized === null) return `is not a valid ${name} value`;
  if (normalized !== value) return `is not normalized (${name}): expected ${JSON.stringify(normalized)}`;
  return null;
}
