/**
 * CONTRACTS §4 normalizers. The writer only checks that identity values are already normalized.
 *
 * The worker implements the same rules character for character (apps/worker/rebox_worker/normalize.py);
 * the shared cases in contracts/normalize-cases.json pin them down and both test suites run them. Hence
 * no `URL` parser (WHATWG parsing punycodes hosts and has no Python twin), case-insensitive regexes
 * only over ASCII (no `u` flag), an explicit whitespace set, and anchored matches. Every normalizer is
 * idempotent: normalize(normalize(x)) === normalize(x).
 */
export type NormalizerName = 'qid' | 'handle' | 'domain' | 'url' | 'email' | 'exact';

/** Whitespace trimmed from both ends (the same set as the Python twin, not String#trim's). */
const WS =
  ' \t\n\r\f\v\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009' +
  '\u200a\u2028\u2029\u202f\u205f\u3000\ufeff';
const WS_SET = new Set(WS);
const QID_RE = /^q([0-9]+)$/i;
const WIKIDATA_ENTITY_RE = /^(?:https?:\/\/)?(?:www\.)?wikidata\.org\/(?:wiki|entity)\/(q[0-9]+)\/?$/i;
// "x.com/jack", "https://github.com/octocat/", "twitter.com/@jack?lang=en" (prefix match)
const HANDLE_URL_RE = /^(?:[a-z][a-z0-9+.-]*:\/\/)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+\/+([^/?#]+)/i;
const SCHEME_RE = /^([a-z][a-z0-9+.-]*):\/\//i;
const PORT_RE = /^[0-9]*$/;

class Invalid extends Error {}

function stripLeft(s: string, chars: Set<string>): string {
  let i = 0;
  while (i < s.length && chars.has(s[i]!)) i++;
  return s.slice(i);
}

function stripRight(s: string, chars: Set<string>): string {
  let j = s.length;
  while (j > 0 && chars.has(s[j - 1]!)) j--;
  return s.slice(0, j);
}

const trim = (s: string): string => stripRight(stripLeft(s, WS_SET), WS_SET);
const hasWs = (s: string): boolean => [...s].some((ch) => WS_SET.has(ch));

function requireString(value: unknown): string {
  if (typeof value !== 'string') throw new Invalid('expected a string');
  const s = trim(value);
  if (!s) throw new Invalid('empty value');
  return s;
}

/** Index of the first of `chars` in `s`, or s.length. */
function firstOf(s: string, chars: string): number {
  let end = s.length;
  for (const ch of chars) {
    const i = s.indexOf(ch);
    if (i !== -1) end = Math.min(end, i);
  }
  return end;
}

/** Drop leading `www.` labels while the rest still contains a dot (`www.com` stays). */
function stripWww(host: string): string {
  while (host.startsWith('www.') && host.slice(4).includes('.')) host = host.slice(4);
  return host;
}

/** [scheme or "", authority without userinfo, rest] of a URL-ish string. */
function splitAuthority(s: string): [string, string, string] {
  const m = SCHEME_RE.exec(s);
  const scheme = m ? m[1]!.toLowerCase() : '';
  const rest = m ? s.slice(m[0].length) : s;
  const end = firstOf(rest, '/?#');
  const authority = rest.slice(0, end);
  return [scheme, authority.slice(authority.lastIndexOf('@') + 1), rest.slice(end)];
}

/**
 * Lowercased host (trailing dots and leading `www.` dropped) and port (digits, no leading zeros) of
 * an authority. An IPv6 literal keeps its brackets.
 */
function hostPort(authority: string): [string, string | null] {
  let host: string;
  let port: string | null;
  if (authority.startsWith('[')) {
    const end = authority.indexOf(']');
    if (end === -1) throw new Invalid('bad IPv6 host');
    host = authority.slice(0, end + 1).toLowerCase();
    const portPart = authority.slice(end + 1);
    if (portPart && !portPart.startsWith(':')) throw new Invalid('bad host');
    port = portPart ? portPart.slice(1) : null;
  } else {
    const colon = authority.indexOf(':');
    const rawHost = colon === -1 ? authority : authority.slice(0, colon);
    port = colon === -1 ? null : authority.slice(colon + 1);
    host = stripWww(stripRight(rawHost.toLowerCase(), new Set('.')));
    if (host.includes('[') || host.includes(']')) throw new Invalid('bad host');
  }
  if (port !== null) {
    if (!PORT_RE.test(port)) throw new Invalid('bad port');
    if (port === '') port = null;
    else {
      const n = Number(port);
      if (n > 65535) throw new Invalid('bad port');
      port = String(n);
    }
  }
  if (!host || hasWs(host)) throw new Invalid('bad host');
  return [host, port];
}

const SLASH_OR_WS = new Set('/' + WS);

const rules: Record<NormalizerName, (raw: unknown) => string> = {
  qid: (raw) => {
    let s = requireString(raw);
    const w = WIKIDATA_ENTITY_RE.exec(s);
    if (w) s = w[1]!;
    const m = QID_RE.exec(s);
    if (!m) throw new Invalid('not a Wikidata QID');
    return `Q${m[1]}`;
  },
  handle: (raw) => {
    let s = requireString(raw);
    const m = HANDLE_URL_RE.exec(s);
    if (m) s = m[1]!;
    s = trim(stripLeft(s, new Set('@' + WS))).toLowerCase();
    if (!s || s.includes('/')) throw new Invalid('not a handle');
    return s;
  },
  domain: (raw) => {
    const [, authority] = splitAuthority(requireString(raw));
    const [host] = hostPort(authority);
    if (host.startsWith('[') || !host.includes('.') || host.split('.').includes('')) {
      throw new Invalid('not a domain');
    }
    return host;
  },
  url: (raw) => {
    const [scheme, authority, rest] = splitAuthority(requireString(raw));
    const [host, port] = hostPort(authority);
    const path = stripRight(rest.slice(0, firstOf(rest, '?#')), SLASH_OR_WS);
    return `${scheme || 'https'}://${port === null ? host : `${host}:${port}`}${path}`;
  },
  email: (raw) => {
    const s = requireString(raw).toLowerCase();
    if (!s.includes('@')) throw new Invalid('not an email');
    return s;
  },
  exact: (raw) => requireString(typeof raw === 'number' && Number.isSafeInteger(raw) ? String(raw) : raw),
};

/** Each normalizer returns the normalized value, or null when the value cannot be normalized. */
export const normalizers = Object.fromEntries(
  (Object.keys(rules) as NormalizerName[]).map((name) => [
    name,
    (raw: unknown): string | null => {
      try {
        return rules[name](raw);
      } catch (e) {
        if (e instanceof Invalid) return null;
        throw e;
      }
    },
  ]),
) as Record<NormalizerName, (raw: unknown) => string | null>;

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
