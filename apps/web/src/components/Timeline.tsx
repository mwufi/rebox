/** Entity timeline (CONTRACTS §14): a vertical register grouped by year, newest first. */
import { useId, useState, type ReactNode } from 'react';
import type { Timeline as TimelineData, TimelineEvent } from '../api';
import { relativeTime } from '../lib/format';
import { eventDate, eventKey, eventSources, groupByYear, hostOf, isCluster, isHeadline, sourceCount, type SourcedEvent } from '../lib/timeline';
import { EntityLink, FactProvenance, SourceBadge } from './bits';

const PREVIEW = 12;

/** One small glyph per event kind (16-unit grid, stroked like the rest of the icon set). */
const GLYPHS: Record<string, ReactNode> = {
  founded: <path d="M4 14V2.5M4 3h7.5l-1.8 2.5L11.5 8H4" />,
  joined: <path d="M2 8h8M7 5l3 3-3 3M13 2.5v11" />,
  left: <path d="M6 8h8M11 5l3 3-3 3M3 2.5v11" />,
  started: <path d="M2 8h8M7 5l3 3-3 3M13 2.5v11" />,
  ended: <path d="M6 8h8M11 5l3 3-3 3M3 2.5v11" />,
  launched: <path d="M8 1.8c2.4 1.6 3.4 4.2 3 7.6l-1.6 2.1H6.6L5 9.4c-.4-3.4.6-6 3-7.6ZM6.6 11.5 5.5 14M9.4 11.5l1.1 2.5M8 6.2v.01" />,
  posted: <path d="M2.5 3.5h11v7H7.5L4.5 13v-2.5h-2Z" />,
  attended: <path d="M2.5 5h11v2a1.2 1.2 0 0 0 0 2.4v2H2.5v-2a1.2 1.2 0 0 0 0-2.4ZM9.5 5v6.4" />,
  acquired: <path d="M3 3v4a3 3 0 0 0 3 3h7M10 7l3 3-3 3M13 3v.01" />,
  invested: <path d="M8 2v12M11 4.5H6.8a1.8 1.8 0 0 0 0 3.6h2.4a1.8 1.8 0 0 1 0 3.6H5" />,
  milestone: <path d="M8 2 13 8l-5 6-5-6Z" />,
  first_seen: <path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8ZM8 6.2a1.8 1.8 0 1 0 0 3.6 1.8 1.8 0 0 0 0-3.6Z" />,
};

export function EventGlyph({ kind }: { kind: string }) {
  return (
    <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {GLYPHS[kind] ?? <circle cx="8" cy="8" r="2.5" />}
    </svg>
  );
}

/** The title with the linked entity: the other entity's name inside the title becomes its link. */
function Title({ e }: { e: TimelineEvent }) {
  const others = e.others?.length ? e.others : e.other ? [e.other] : [];
  const parts: ReactNode[] = [];
  let rest = e.title;
  for (const o of others) {
    const name = o.name ?? o.id;
    const at = rest.indexOf(name);
    if (at < 0) continue;
    if (at > 0) parts.push(rest.slice(0, at));
    parts.push(<EntityLink key={o.id} entity={o} />);
    rest = rest.slice(at + name.length);
  }
  if (rest) parts.push(rest);
  // No name found in the title (e.g. a renamed entity): append the link instead.
  if (parts.length === 1 && others[0]) parts.push(' · ', <EntityLink key={others[0].id} entity={others[0]} />);
  return <>{parts}</>;
}

/** A clustered milestone's "N sources" toggle and the list it opens (S18). */
function Sources({ e, open, onToggle, listId }: { e: SourcedEvent; open: boolean; onToggle: () => void; listId: string }) {
  return (
    <button
      type="button"
      className="tl-src-link tl-src-toggle"
      aria-expanded={open}
      aria-controls={listId}
      onClick={onToggle}
      style={{ background: 'none', border: 0, padding: 0, cursor: 'pointer', textDecoration: 'underline dotted' }}
    >
      {sourceCount(e)} sources {open ? '▴' : '▾'}
    </button>
  );
}

function Event({ e }: { e: SourcedEvent }) {
  const head = isHeadline(e);
  const date = eventDate(e);
  const fact = e.fact;
  const cluster = isCluster(e);
  const sources = eventSources(e);
  const [open, setOpen] = useState(false);
  const listId = useId();
  const milestoneUrl = !cluster && e.kind === 'milestone' ? (sources[0]?.url ?? null) : null;
  return (
    <li className={`tl-event${head ? ' headline' : ''}`} data-kind={e.kind}>
      <span className="tl-mark" aria-hidden="true">
        <EventGlyph kind={e.kind} />
      </span>
      <time className="tl-date" dateTime={e.at} title={date.full}>
        {date.short}
      </time>
      <div className="tl-body">
        <span className="tl-title">
          <Title e={e} />
        </span>
        <span className="tl-meta">
          {e.kind === 'first_seen' ? (
            <span className="mono">
              {relativeTime(e.at)}
              {e.layer ? ` · ${e.layer}` : ''}
            </span>
          ) : null}
          {milestoneUrl && (
            <a href={milestoneUrl} target="_blank" rel="noopener noreferrer nofollow" className="tl-src-link">
              Link ↗
            </a>
          )}
          {cluster && <Sources e={e} open={open} onToggle={() => setOpen((o) => !o)} listId={listId} />}
          {fact && <FactProvenance fact={fact} />}
          {fact && <SourceBadge fact={fact} />}
        </span>
        {cluster && open && (
          <ul
            id={listId}
            className="tl-sources"
            aria-label={`${sources.length} sources`}
            style={{ flexBasis: '100%', margin: '2px 0 0', padding: 0, listStyle: 'none', fontSize: 12, lineHeight: 1.5 }}
          >
            {sources.map((s) => (
              <li key={s.url} style={{ overflowWrap: 'anywhere' }}>
                <a href={s.url} target="_blank" rel="noopener noreferrer nofollow" className="tl-src-link">
                  {hostOf(s.url)} ↗
                </a>{' '}
                <span style={{ color: 'var(--ink-3)' }}>{s.title}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
  );
}

export function TimelineView({ data, loadMore, loadingMore }: { data: TimelineData; loadMore?: () => void; loadingMore?: boolean }) {
  const [all, setAll] = useState(false);
  const events = data.events;
  const shown = all ? events : events.slice(0, PREVIEW);
  const groups = groupByYear(shown);
  const dated = events.filter((e) => e.kind !== 'first_seen').length;
  if (dated === 0) {
    const first = events.find((e) => e.kind === 'first_seen');
    return (
      <p className="empty tl-empty">
        No dated facts yet.
        {first ? ` First seen in Rebox ${relativeTime(first.at)}.` : ''}
      </p>
    );
  }
  return (
    <>
      <ol className="tl" aria-label="Timeline, newest first">
        {groups.map((g) => (
          <li key={g.year} className="tl-year">
            <span className="tl-year-label" aria-label={`Year ${g.year}`}>
              {g.year}
            </span>
            <ol>
              {g.events.map((e, i) => (
                <Event key={eventKey(e, i)} e={e} />
              ))}
            </ol>
          </li>
        ))}
      </ol>
      {(events.length > PREVIEW || data.next) && (
        <button
          type="button"
          className="btn ghost sm more-btn"
          disabled={loadingMore}
          onClick={() => {
            if (!all && events.length > PREVIEW) setAll(true);
            else if (data.next && loadMore) loadMore();
            else setAll(false);
          }}
        >
          {!all && events.length > PREVIEW
            ? `Show all ${events.length}${data.next ? '+' : ''} events`
            : data.next
              ? loadingMore
                ? 'Loading…'
                : 'Load older events'
              : 'Show fewer'}
        </button>
      )}
    </>
  );
}
