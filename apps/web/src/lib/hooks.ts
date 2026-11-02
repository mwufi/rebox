import { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { api as defaultApi, type Client, type Predicate } from '../api';

export const ApiContext = createContext<Client>(defaultApi);
export const useApi = () => useContext(ApiContext);

export interface Async<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
}

/**
 * Small request cache keyed by string: navigating back to a page renders instantly from cache
 * while it refreshes. Entries expire after `ttl` ms.
 */
const cache = new Map<string, { at: number; value: unknown }>();
const TTL = 60_000;

export function primeCache(key: string, value: unknown) {
  cache.set(key, { at: Date.now(), value });
}

export function clearCache(prefix = '') {
  for (const k of [...cache.keys()]) if (k.startsWith(prefix)) cache.delete(k);
}

/** Runs `fn` when `key` changes (null = idle); aborts stale requests; serves cached data first. */
export function useAsync<T>(key: string | null, fn: (signal: AbortSignal) => Promise<T>, opts: { cache?: boolean } = {}): Async<T> {
  const useCache = opts.cache !== false;
  const cached = key && useCache ? (cache.get(key) as { at: number; value: T } | undefined) : undefined;
  const [state, setState] = useState<Async<T> & { key: string | null }>(() => ({
    key,
    data: cached?.value,
    error: undefined,
    loading: key !== null && !cached,
  }));
  const fnRef = useRef(fn);
  fnRef.current = fn;

  // Reset synchronously on key change so a stale page never shows under a new key.
  if (state.key !== key) {
    const next = { key, data: cached?.value, error: undefined, loading: key !== null && !cached };
    setState(next);
  }

  useEffect(() => {
    if (key === null) return;
    const hit = useCache ? cache.get(key) : undefined;
    if (hit && Date.now() - hit.at < TTL) return;
    const ctl = new AbortController();
    fnRef
      .current(ctl.signal)
      .then((value) => {
        if (ctl.signal.aborted) return;
        if (useCache) cache.set(key, { at: Date.now(), value });
        setState({ key, data: value, error: undefined, loading: false });
      })
      .catch((error: unknown) => {
        if (ctl.signal.aborted || (error as Error)?.name === 'AbortError') return;
        setState((s) => ({ key, data: s.key === key ? s.data : undefined, error: error as Error, loading: false }));
      });
    return () => ctl.abort();
  }, [key, useCache]);

  return state.key === key ? state : { data: cached?.value, error: undefined, loading: key !== null && !cached };
}

/** Predicates by id, fetched once. */
export function usePredicates(): { list: Predicate[]; byId: ReadonlyMap<string, Predicate>; loading: boolean } {
  const client = useApi();
  const r = useAsync('predicates', (signal) => client.predicates({ signal }));
  return useMemo(() => {
    const list = r.data ?? [];
    return { list, byId: new Map(list.map((p) => [p.id, p])), loading: r.loading };
  }, [r.data, r.loading]);
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function useTitle(title: string | null | undefined) {
  useEffect(() => {
    document.title = title ? `${title} · Rebox` : 'Rebox — an open people graph';
  }, [title]);
}

type Theme = 'light' | 'dark' | 'system';
const THEME_KEY = 'rebox.theme';

function readTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === 'light' || t === 'dark' ? t : 'system';
  } catch {
    return 'system';
  }
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', theme);
    try {
      if (theme === 'system') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, theme);
    } catch {
      // storage unavailable: the choice lasts for this page view
    }
  }, [theme]);
  const toggle = () => {
    const dark = theme === 'dark' || (theme === 'system' && window.matchMedia?.('(prefers-color-scheme: dark)').matches);
    setTheme(dark ? 'light' : 'dark');
  };
  return [theme, toggle];
}
