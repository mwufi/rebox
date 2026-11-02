import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import type { SearchHit } from '../api';
import { Avatar, EntityName, KindGlyph, KindTag, SkelRows, entityPath } from '../components/bits';
import { KINDS, avatarOf, handleOf, kindLabel } from '../lib/format';
import { useApi, useAsync, useTitle } from '../lib/hooks';

const LIMIT = 60;

/** Groups hits by kind, keeping the kind order of the best hit in each group. */
export function groupHits(hits: SearchHit[]): { kind: string; hits: SearchHit[] }[] {
  const by = new Map<string, SearchHit[]>();
  for (const h of hits) {
    const list = by.get(h.kind);
    if (list) list.push(h);
    else by.set(h.kind, [h]);
  }
  return [...by.entries()].map(([kind, list]) => ({ kind, hits: list }));
}

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get('q') ?? '';
  const kind = params.get('kind') ?? '';
  const client = useApi();
  const navigate = useNavigate();
  const [draft, setDraft] = useState(q);
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  useTitle(q ? `“${q}”` : 'Search');

  useEffect(() => setDraft(q), [q]);

  // Always fetch all kinds (for tab counts); filter client-side.
  const r = useAsync(q.trim() ? `search:${q.trim()}` : null, (signal) => client.search(q.trim(), { limit: LIMIT, signal }));
  const all = r.data ?? [];
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const h of all) c[h.kind] = (c[h.kind] ?? 0) + 1;
    return c;
  }, [all]);
  const shown = kind ? all.filter((h) => h.kind === kind) : all;
  const groups = groupHits(shown);
  const flat = groups.flatMap((g) => g.hits);

  useEffect(() => setActive(0), [q, kind]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      const inField = ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName);
      if (!flat.length) return;
      if (e.key === 'ArrowDown' || (!inField && e.key === 'j')) {
        e.preventDefault();
        setActive((a) => Math.min(flat.length - 1, a + 1));
      } else if (e.key === 'ArrowUp' || (!inField && e.key === 'k')) {
        e.preventDefault();
        setActive((a) => Math.max(0, a - 1));
      } else if (e.key === 'Enter' && (!inField || t.dataset.searchInput === 'true')) {
        const hit = flat[active];
        if (hit && (!inField || draft.trim() === q.trim())) {
          e.preventDefault();
          navigate(entityPath(hit.id));
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [flat, active, navigate, draft, q]);

  useEffect(() => {
    listRef.current?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  const setKind = (k: string) => {
    const p = new URLSearchParams(params);
    if (k) p.set('kind', k);
    else p.delete('kind');
    setParams(p, { replace: true });
  };

  const maxScore = flat[0]?.score ?? 1;
  let idx = -1;

  return (
    <div className="wrap">
      <header className="search-head">
        <span className="eyebrow">Search</span>
        <form
          className="search-bar"
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            const p = new URLSearchParams(params);
            p.set('q', draft.trim());
            setParams(p);
          }}
        >
          <input
            className="field"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Search query"
            placeholder="Name of a person, company, launch…"
            data-search-input="true"
            autoFocus={!q}
          />
          <button type="submit" className="btn primary" style={{ height: 42 }}>
            Search
          </button>
        </form>
        <div className="kind-tabs" role="group" aria-label="Filter by kind">
          <button type="button" aria-pressed={!kind} onClick={() => setKind('')}>
            All <span className="c">{all.length}</span>
          </button>
          {KINDS.map((k) => (
            <button key={k} type="button" aria-pressed={kind === k} onClick={() => setKind(k)} disabled={!counts[k] && kind !== k}>
              <KindGlyph kind={k} />
              {kindLabel(k)} <span className="c">{counts[k] ?? 0}</span>
            </button>
          ))}
        </div>
      </header>

      <div ref={listRef} aria-live="polite">
        {!q.trim() && <p className="empty">Type a name to search the graph.</p>}
        {r.loading && !r.data && (
          <div className="results-group">
            <SkelRows n={6} h={46} className="facts" />
          </div>
        )}
        {r.error && <p className="empty">Search failed: {r.error.message}</p>}
        {r.data && flat.length === 0 && (
          <p className="empty">
            No {kind ? kindLabel(kind) : 'matches'} for “{q}”. Try fewer letters, or <Link to="/query">build a query</Link>.
          </p>
        )}
        {groups.map((g) => (
          <section key={g.kind} className="results-group fade-in" aria-label={kindLabel(g.kind)}>
            <div className="section-head">
              <h2>
                <KindTag kind={g.kind} plural />
              </h2>
              <span className="count">{g.hits.length}</span>
            </div>
            <ul className="result-list">
              {g.hits.map((h) => {
                idx += 1;
                const i = idx;
                return (
                  <li key={h.id}>
                    <Link
                      to={entityPath(h.id)}
                      className="result-row"
                      data-active={i === active}
                      onMouseEnter={() => setActive(i)}
                      aria-current={i === active ? 'true' : undefined}
                    >
                      <span className="elink" data-kind={h.kind}>
                        {avatarOf(h) ? <Avatar entity={h} size={22} /> : <KindGlyph kind={h.kind} />}
                        <EntityName entity={h} handle={handleOf(h)} />
                      </span>
                      <span className="id" title={`score ${h.score}`}>
                        {h.id}
                        <span className="score-bar" aria-hidden="true">
                          <i style={{ width: `${Math.max(6, Math.round((h.score / (maxScore || 1)) * 100))}%` }} />
                        </span>
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
        {flat.length > 0 && (
          <p className="hint-line">
            <span>
              <span className="kbd">↑</span> <span className="kbd">↓</span> or <span className="kbd">j</span> <span className="kbd">k</span> to move
            </span>
            <span>
              <span className="kbd">↵</span> to open
            </span>
            <span>
              <span className="kbd">/</span> to search again
            </span>
          </p>
        )}
      </div>
    </div>
  );
}
