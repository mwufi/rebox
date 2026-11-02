import { useEffect, useState, type CSSProperties } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { ApiError, isRedirect, type EntityView, type Fact } from '../api';
import { Avatar, CopyButton, EntityLink, FactProvenance, HandleHint, Icon, KindTag, Skel, SkelRows, SourceBadge, graphPath } from '../components/bits';
import { TimelineView } from '../components/Timeline';
import { WikiSection } from '../components/Wiki';
import { compareEdges, edgeTotal, groupFacts, sourcesOf, type FactGroup, type PredicateIndex } from '../lib/facts';
import { avatarOf, displayName, edgeProps, factWhen, handleOf, hrefFor, plural, sourceCode, sourceHue, valueText } from '../lib/format';
import { primeCache, useApi, useAsync, usePredicates, useTitle } from '../lib/hooks';
import { NotFound } from './NotFound';

const EDGE_PREVIEW = 8;

function Value({ fact }: { fact: Fact }) {
  const text = valueText(fact.value);
  const href = hrefFor(fact.predicate, fact.value);
  if (href) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer nofollow" title={text}>
        {text.replace(/^https?:\/\//, '')}
      </a>
    );
  }
  return <span title={text}>{text}</span>;
}

/** Edges fetched per click when a group has more than the view carried (S13). */
const EDGE_PAGE = 100;

export function EdgeGroup({ group, side, entityId }: { group: FactGroup; side: 'object' | 'subject'; entityId: string }) {
  const client = useApi();
  const [all, setAll] = useState(false);
  // Edges beyond the ones the view carried, loaded from /v0/entities/:id/edges in its order.
  const [more, setMore] = useState<{ facts: Fact[]; next: string | null; started: boolean }>({ facts: [], next: null, started: false });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setAll(false);
    setMore({ facts: [], next: null, started: false });
    setError(null);
  }, [entityId, group.predicate]);

  const otherOf = (f: Fact) => (side === 'object' ? f.object! : f.subject);
  const keyOf = (f: Fact) => `${f.predicate}|${otherOf(f).id}`;
  const loaded = (() => {
    const seen = new Set(group.facts.map(keyOf));
    const extra = more.facts.filter((f) => !seen.has(keyOf(f)) && (seen.add(keyOf(f)), true));
    return extra.length ? [...group.facts, ...extra].sort((a, b) => compareEdges(a, b, side)) : group.facts;
  })();
  const total = Math.max(group.total, loaded.length);
  const remaining = total - loaded.length;
  const facts = all ? loaded : loaded.slice(0, EDGE_PREVIEW);

  const loadMore = async () => {
    setLoading(true);
    setError(null);
    try {
      // The first page starts from the beginning (the view carried its first edges; they dedupe).
      const page = await client.edges(entityId, {
        predicate: group.predicate,
        dir: side === 'object' ? 'out' : 'in',
        limit: more.started ? EDGE_PAGE : group.facts.length + EDGE_PAGE,
        cursor: more.started ? more.next : null,
      });
      setMore((m) => ({ facts: [...m.facts, ...page.edges], next: page.next, started: true }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };
  const canFetch = remaining > 0 && (!more.started || more.next !== null);

  return (
    <section className="edge-group" aria-label={group.label} data-predicate={group.predicate}>
      <div className="section-head">
        <h3>{group.label}</h3>
        <span className="count">{total.toLocaleString('en-US')}</span>
      </div>
      <ul className="facts">
        {facts.map((f) => {
          const other = otherOf(f);
          const props = edgeProps(f.value);
          const when = factWhen(f);
          return (
            <li key={`${f.predicate}|${other.id}|${f.layer}`}>
              <span className="main">
                <EntityLink entity={other} />
                {props && <span className="props">{props}</span>}
                <FactProvenance fact={f} />
              </span>
              <span className={`when${when.current ? ' current' : ''}`}>{when.text}</span>
              <SourceBadge fact={f} />
            </li>
          );
        })}
      </ul>
      {error && <p className="empty">Could not load more: {error}</p>}
      {!all && total > EDGE_PREVIEW && (
        <button
          type="button"
          className="btn ghost sm more-btn"
          onClick={() => {
            setAll(true);
            if (canFetch) void loadMore();
          }}
        >
          {remaining > 0 ? `+${(total - EDGE_PREVIEW).toLocaleString('en-US')} more` : `Show all ${total}`}
        </button>
      )}
      {all && canFetch && (
        <button type="button" className="btn ghost sm more-btn" disabled={loading} onClick={loadMore}>
          {loading ? 'Loading…' : `+${remaining.toLocaleString('en-US')} more`}
        </button>
      )}
      {all && !canFetch && total > EDGE_PREVIEW && (
        <button type="button" className="btn ghost sm more-btn" onClick={() => setAll(false)}>
          Show fewer
        </button>
      )}
    </section>
  );
}

function KeyAttrs({ groups }: { groups: FactGroup[] }) {
  const items = groups.filter((g) => g.predicate !== 'description' && g.predicate !== 'headline');
  if (!items.length) return null;
  return (
    <dl className="keyattrs" aria-label="Key attributes">
      {items.map((g) => (
        <div key={g.predicate}>
          <dt className="eyebrow">{g.label}</dt>
          {g.facts.map((f) => (
            <dd key={`${f.layer}|${valueText(f.value)}`}>
              <span className="v num">
                <Value fact={f} />
              </span>
              <SourceBadge fact={f} />
              <FactProvenance fact={f} />
            </dd>
          ))}
        </div>
      ))}
    </dl>
  );
}

function AttrList({ groups }: { groups: FactGroup[] }) {
  return (
    <dl className="attr-list">
      {groups.map((g) => (
        <div key={g.predicate}>
          <dt>{g.label}</dt>
          <dd>
            {g.facts.map((f) => (
              <span className="val-wrap" key={`${f.layer}|${valueText(f.value)}`}>
                <span className="val">
                  <Value fact={f} />
                  <SourceBadge fact={f} />
                </span>
                <FactProvenance fact={f} />
              </span>
            ))}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Provenance({ view }: { view: EntityView }) {
  const sources = sourcesOf(view);
  const max = sources[0]?.n ?? 1;
  return (
    <ul className="prov-bars">
      {sources.map((s) => (
        <li key={s.layer} style={{ '--h': sourceHue(s.source) } as CSSProperties}>
          <span className="src" style={{ '--h': sourceHue(s.source) } as CSSProperties} title={s.layer}>
            {sourceCode(s.source)}
          </span>
          <span className="bar" title={`${s.layer}: ${plural(s.n, 'fact')}`}>
            <i style={{ width: `${(s.n / max) * 100}%` }} />
          </span>
          <span className="mono muted num">
            {s.layer} · {s.n}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function EntityBody({ view, preds }: { view: EntityView; preds: PredicateIndex }) {
  const g = groupFacts(view, preds);
  const desc = g.key.find((x) => x.predicate === 'description') ?? g.key.find((x) => x.predicate === 'headline');
  const descFact = desc?.facts[0];
  const degree = edgeTotal(view);
  const handle = handleOf(view);
  const hasAvatar = avatarOf(view) !== null;

  return (
    <>
      <header className="entity-head fade-in">
        <div className="crumbs">
          <KindTag kind={view.kind} />
          <span className="id">
            {view.id}
            <CopyButton text={view.id} label="Copy id" />
          </span>
        </div>
        <div className={`entity-title${hasAvatar ? ' with-avatar' : ''}`}>
          {hasAvatar && <Avatar entity={view} size={64} className="entity-avatar" />}
          <h1 className={`entity-name${handle ? ' is-handle' : ''}`}>{displayName(view)}</h1>
          {handle && <HandleHint kind={handle} />}
        </div>
        {descFact && (
          <p className="entity-desc">
            <span>{valueText(descFact.value)}</span>
            <SourceBadge fact={descFact} />
            <FactProvenance fact={descFact} />
          </p>
        )}
        <div className="entity-actions">
          <Link to={graphPath(view.id)} className="btn">
            <Icon name="graph" /> Neighborhood graph
          </Link>
          <a className="btn ghost" href={`/v0/entities/${encodeURIComponent(view.id)}`} target="_blank" rel="noreferrer">
            JSON <Icon name="ext" size={12} />
          </a>
        </div>
      </header>

      <KeyAttrs groups={g.key} />

      <div className="entity-body">
        <div>
          <WikiSection id={view.id} name={displayName(view)} />
          <TimelineSection id={view.id} />
          {g.outgoing.length === 0 && g.incoming.length === 0 && <p className="empty">No connections recorded yet.</p>}
          {g.outgoing.map((grp) => (
            <EdgeGroup key={`out:${grp.predicate}`} group={grp} side="object" entityId={view.id} />
          ))}
          {g.incoming.map((grp) => (
            <EdgeGroup key={`in:${grp.predicate}`} group={grp} side="subject" entityId={view.id} />
          ))}
        </div>
        <aside>
          {view.merged_from.length > 0 && (
            <div className="side-block merged-note" role="note">
              Merged from {plural(view.merged_from.length, 'record')}; their facts appear here.
              <ul>
                {view.merged_from.map((id) => (
                  <li key={id}>{id}</li>
                ))}
              </ul>
            </div>
          )}
          {g.attributes.length > 0 && (
            <div className="side-block">
              <div className="section-head">
                <h3>Attributes</h3>
              </div>
              <AttrList groups={g.attributes} />
            </div>
          )}
          <div className="side-block">
            <div className="section-head">
              <h3>Provenance</h3>
              <span className="count">{plural(g.total + 1, 'fact')}</span>
            </div>
            <Provenance view={view} />
          </div>
          <Link to={graphPath(view.id)} className="side-block mini-graph" aria-label="Open the neighborhood graph">
            <MiniGraph view={view} />
            <span className="label">
              {plural(degree, 'connection')} · open graph →
            </span>
          </Link>
        </aside>
      </div>
    </>
  );
}

function TimelineSection({ id }: { id: string }) {
  const client = useApi();
  const r = useAsync(`timeline:${id}`, (signal) => client.timeline(id, { signal }));
  const [more, setMore] = useState<{ events: NonNullable<typeof r.data>['events']; next: string | null } | null>(null);
  const [loading, setLoading] = useState(false);
  useEffect(() => setMore(null), [id]);
  const data = r.data ? { ...r.data, events: [...r.data.events, ...(more?.events ?? [])], next: more ? more.next : (r.data.next ?? null) } : null;
  const loadMore = async () => {
    if (!data?.next) return;
    setLoading(true);
    try {
      const page = await client.timeline(id, { cursor: data.next });
      setMore((m) => ({ events: [...(m?.events ?? []), ...page.events], next: page.next ?? null }));
    } finally {
      setLoading(false);
    }
  };
  const dated = data ? data.events.filter((e) => e.kind !== 'first_seen').length : 0;
  return (
    <section className="timeline-section" aria-labelledby={`tl-h-${id}`}>
      <div className="section-head">
        <h2 id={`tl-h-${id}`}>Timeline</h2>
        {data && <span className="count">{plural(dated, 'event')}</span>}
      </div>
      {r.loading && !data && <SkelRows n={3} h={38} />}
      {r.error && !data && <p className="empty">Could not load the timeline: {r.error.message}</p>}
      {data && <TimelineView data={data} loadMore={loadMore} loadingMore={loading} />}
    </section>
  );
}

/** A static radial thumbnail of the 1-hop neighborhood (no simulation). */
function MiniGraph({ view }: { view: EntityView }) {
  const others = [
    ...view.facts.filter((f) => f.object).map((f) => f.object!),
    ...view.incoming.map((f) => f.subject),
  ];
  const uniq = [...new Map(others.map((o) => [o.id, o])).values()].slice(0, 18);
  const W = 300;
  const H = 225;
  const cx = W / 2;
  const cy = H / 2 - 8;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height="100%" aria-hidden="true">
      {uniq.map((o, i) => {
        const a = (i / Math.max(1, uniq.length)) * Math.PI * 2 - Math.PI / 2;
        const r = 70 + (i % 2) * 18;
        const x = cx + Math.cos(a) * r * 1.25;
        const y = cy + Math.sin(a) * r * 0.85;
        return (
          <g key={o.id}>
            <line x1={cx} y1={cy} x2={x} y2={y} stroke="var(--rule)" />
            <circle cx={x} cy={y} r={4.5} fill={`var(--k-${['person', 'company', 'launch', 'event', 'place'].includes(o.kind) ? o.kind : 'other'})`} />
          </g>
        );
      })}
      <circle cx={cx} cy={cy} r={8} fill={`var(--k-${view.kind})`} stroke="var(--ink)" strokeWidth={1.5} />
    </svg>
  );
}

function EntitySkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading entity">
      <header className="entity-head">
        <div className="crumbs">
          <Skel w={70} h={11} />
          <Skel w={220} h={11} />
        </div>
        <Skel w="min(520px, 80%)" h={46} style={{ marginTop: 6 }} />
        <Skel w="min(640px, 90%)" h={16} style={{ marginTop: 18 }} />
        <div className="entity-actions">
          <Skel w={170} h={32} />
        </div>
      </header>
      <div className="keyattrs">
        {[0, 1, 2].map((i) => (
          <div key={i}>
            <Skel w={60} h={9} style={{ marginBottom: 8 }} />
            <Skel w={110} h={14} />
          </div>
        ))}
      </div>
      <div className="entity-body">
        <div>
          <div className="section-head">
            <Skel w={120} h={18} />
          </div>
          <SkelRows n={5} />
        </div>
        <aside>
          <div className="section-head">
            <Skel w={90} h={18} />
          </div>
          <SkelRows n={4} h={34} />
        </aside>
      </div>
    </div>
  );
}

export function EntityPage() {
  const { id = '' } = useParams();
  const client = useApi();
  const navigate = useNavigate();
  const location = useLocation();
  const preds = usePredicates();
  const r = useAsync(`entity:${id}`, (signal) => client.entity(id, { signal }));

  const view = r.data ? (isRedirect(r.data) ? r.data.entity : r.data) : undefined;
  const mergedFrom = (location.state as { from?: string } | null)?.from ?? (r.data && isRedirect(r.data) ? id : null);
  useTitle(view ? displayName(view) : null);

  // Follow merge redirects: show the target under its canonical URL (primed, so no refetch).
  useEffect(() => {
    if (r.data && isRedirect(r.data)) {
      primeCache(`entity:${r.data.redirect}`, r.data.entity);
      navigate(`/e/${encodeURIComponent(r.data.redirect)}`, { replace: true, state: { from: id } });
    }
  }, [r.data, id, navigate]);

  if (r.error instanceof ApiError && r.error.status === 404) return <NotFound what={`No entity with id ${id}.`} />;

  return (
    <div className="wrap">
      {mergedFrom && mergedFrom !== view?.id && (
        <p className="notice" style={{ marginTop: 18 }}>
          <span className="mono">{mergedFrom}</span> was merged into this entity.
        </p>
      )}
      {r.error && <p className="empty">Could not load: {r.error.message}</p>}
      {!view && !r.error && <EntitySkeleton />}
      {view && <EntityBody view={view} preds={preds.byId} />}
    </div>
  );
}
