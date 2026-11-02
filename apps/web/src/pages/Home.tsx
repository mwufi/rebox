import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { Change, EntityRef } from '../api';
import { EntityLink, Icon, KindTag, Skel, SkelRows, entityPath } from '../components/bits';
import { Suggest } from '../components/EntityPicker';
import { KINDS, kindLabel, plural, relativeTime } from '../lib/format';
import { useApi, useAsync, useTitle } from '../lib/hooks';
import { encodeRow, type Row } from '../lib/queryState';

const RECENT = 12;

function queryHref(find: string, rows: Row[]) {
  const p = new URLSearchParams({ find });
  rows.forEach((r) => p.append('w', encodeRow(r)));
  return `/query?${p}`;
}

export const EXAMPLES: { q: string; href: string; code: string }[] = [
  {
    q: 'Everyone who founded a company',
    href: queryHref('person', [{ type: 'edge', predicate: 'founded', dir: 'out', to: '', from: '', until: '' }]),
    code: 'find person · founded → *',
  },
  {
    q: 'Companies founded in 2020 or later',
    href: queryHref('company', [{ type: 'attr', predicate: 'founded_year', op: 'gte', value: '2020' }]),
    code: 'find company · founded_year ≥ 2020',
  },
  {
    q: 'People with a job that overlaps 2018–2021',
    href: queryHref('person', [{ type: 'edge', predicate: 'worked_at', dir: 'out', to: '', from: '2018-01-01', until: '2021-12-31' }]),
    code: 'find person · worked_at during 2018–2021',
  },
  {
    q: 'Companies that shipped a launch',
    href: queryHref('company', [{ type: 'edge', predicate: 'launched', dir: 'out', to: '', from: '', until: '' }]),
    code: 'find company · launched → *',
  },
];

interface Recent {
  entity: EntityRef;
  change: Change;
  n: number;
}

function describe(c: Change): string {
  if (c.op === 'merge') return 'merged';
  if (c.op === 'split') return 'split';
  const verb = c.op === 'insert' ? 'added' : c.op === 'update' ? 'updated' : 'removed';
  return `${c.predicate ?? 'fact'} ${verb}`;
}

function useRecent() {
  const client = useApi();
  return useAsync('home:recent', async (signal) => {
    const stats = await client.stats({ signal });
    const after = Math.max(0, stats.last_seq - 400);
    const { changes } = await client.changes({ after, limit: 400, signal });
    // Newest first, one row per subject.
    const latest = new Map<string, { change: Change; n: number }>();
    for (const c of [...changes].reverse()) {
      if (!c.subject) continue;
      const cur = latest.get(c.subject);
      if (cur) cur.n += 1;
      else latest.set(c.subject, { change: c, n: 1 });
    }
    const top = [...latest.entries()].slice(0, RECENT * 2);
    const refs = await client.entities(
      top.map(([id]) => id),
      { signal },
    );
    // /v0/entities answers in request order, following merges and omitting unknown ids. When
    // nothing was omitted, zip by position (covers merged ids); otherwise match by id.
    const aligned = refs.length === top.length;
    const byId = new Map(refs.map((r) => [r.id, r]));
    const seen = new Set<string>();
    const out: Recent[] = [];
    top.forEach(([id, v], i) => {
      const ref = aligned ? refs[i] : byId.get(id);
      if (!ref || seen.has(ref.id) || out.length >= RECENT) return;
      seen.add(ref.id);
      out.push({ entity: ref, ...v });
    });
    return { stats, recent: out };
  });
}

function BigSearch() {
  const [q, setQ] = useState('');
  const [kind, setKind] = useState('');
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  return (
    <div className="bigsearch">
      <form
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          if (!q.trim()) return;
          const p = new URLSearchParams({ q: q.trim() });
          if (kind) p.set('kind', kind);
          navigate(`/search?${p}`);
        }}
      >
        <Suggest
          q={q}
          kind={kind || undefined}
          open={open}
          onClose={() => setOpen(false)}
          onPick={(h) => navigate(entityPath(h.id))}
        >
          {({ onKeyDown, listId, activeId, expanded }) => (
            <input
              autoFocus
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setOpen(true);
              }}
              onFocus={() => setOpen(true)}
              onBlur={() => setTimeout(() => setOpen(false), 120)}
              onKeyDown={onKeyDown}
              placeholder="Search a founder, a company, a launch…"
              aria-label="Search the graph"
              role="combobox"
              aria-controls={listId}
              aria-expanded={expanded}
              aria-activedescendant={activeId}
              aria-autocomplete="list"
            />
          )}
        </Suggest>
        <select className="field" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind">
          <option value="">All kinds</option>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {kindLabel(k)}
            </option>
          ))}
        </select>
        <button type="submit">Search</button>
      </form>
    </div>
  );
}

export function HomePage() {
  useTitle(null);
  const r = useRecent();
  const stats = r.data?.stats;
  const total = stats ? Object.values(stats.entities).reduce((a, b) => a + b, 0) : 0;

  return (
    <div className="wrap">
      <section className="hero">
        <div className="hero-kicker">
          <span className="eyebrow">An open graph · San Francisco startups</span>
        </div>
        <h1>
          Who built what, <em>with whom,</em> and when.
        </h1>
        <p className="lede">
          People, companies and launches, assembled from public sources. Each fact is a claim you can trace back to where it came from.
        </p>
        <BigSearch />
      </section>

      <section aria-label="Graph size">
        <div className="register">
          <div>
            {stats ? <span className="n">{total.toLocaleString('en-US')}</span> : <Skel w={72} h={34} />}
            <span className="l eyebrow">entities</span>
          </div>
          {(['person', 'company', 'launch'] as const).map((k) => (
            <div key={k}>
              {stats ? <span className="n">{(stats.entities[k] ?? 0).toLocaleString('en-US')}</span> : <Skel w={56} h={34} />}
              <span className="l eyebrow" data-kind={k}>
                <span className="kind-glyph" data-kind={k} aria-hidden="true" />
                {kindLabel(k)}
              </span>
            </div>
          ))}
          <div>
            {stats ? <span className="n">{stats.claims.toLocaleString('en-US')}</span> : <Skel w={72} h={34} />}
            <span className="l eyebrow">claims</span>
          </div>
          <div>
            {stats ? <span className="n">{stats.layers.length}</span> : <Skel w={30} h={34} />}
            <span className="l eyebrow">sources</span>
          </div>
        </div>
      </section>

      <div className="home-grid">
        <section aria-labelledby="recent-h">
          <div className="section-head">
            <h2 id="recent-h">Recently changed</h2>
            {stats && <span className="count">seq {stats.last_seq.toLocaleString('en-US')}</span>}
          </div>
          {r.error && <p className="empty">Could not load changes: {r.error.message}</p>}
          {!r.data && !r.error && <SkelRows n={8} h={43} className="ledger" />}
          {r.data && r.data.recent.length === 0 && <p className="empty">Nothing yet. The graph is empty until a connector runs.</p>}
          {r.data && r.data.recent.length > 0 && (
            <ul className="ledger fade-in">
              {r.data.recent.map(({ entity, change, n }) => (
                <li key={entity.id}>
                  <span className="seq">#{change.seq}</span>
                  <span style={{ minWidth: 0 }}>
                    <EntityLink entity={entity} />
                    <span className="what">
                      {describe(change)}
                      {n > 1 ? ` · ${plural(n, 'change')}` : ''}
                      {change.layer_id ? ` · ${change.layer_id}` : ''}
                    </span>
                  </span>
                  <span className="meta">
                    <KindTag kind={entity.kind} />
                    <br />
                    {relativeTime(change.at)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-labelledby="ex-h">
          <div className="section-head">
            <h2 id="ex-h">Try a query</h2>
            <Link to="/query" className="aside">
              Builder →
            </Link>
          </div>
          <ol className="examples">
            {EXAMPLES.map((ex) => (
              <li key={ex.href}>
                <Link to={ex.href}>
                  <span className="q">{ex.q}</span>
                  <span className="arrow">
                    <Icon name="arrow" />
                  </span>
                  <code>{ex.code}</code>
                </Link>
              </li>
            ))}
          </ol>

          {stats && stats.layers.length > 0 && (
            <div style={{ marginTop: 32 }}>
              <div className="section-head">
                <h3>Sources</h3>
                <span className="count">{plural(stats.layers.length, 'layer')}</span>
              </div>
              <ul className="ledger">
                {stats.layers.map((l) => (
                  <li key={l.id} style={{ gridTemplateColumns: '1fr auto', minHeight: 36 }}>
                    <span className="mono">{l.id}</span>
                    <span className="meta num">{plural(l.claims, 'claim')}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
