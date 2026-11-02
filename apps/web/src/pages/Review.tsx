/** /review — admin review queue. The admin token is kept in sessionStorage only. */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, isRedirect, type EntityRef, type EntityView, type ReviewItem } from '../api';
import { KindTag, Skel, SourceBadge, entityPath } from '../components/bits';
import { KEY_ATTRS, labelOf } from '../lib/facts';
import { clearCache, useApi, usePredicates, useTitle } from '../lib/hooks';
import { displayName, plural, relativeTime, valueText } from '../lib/format';

export const TOKEN_KEY = 'rebox.adminToken';

function readToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

function saveToken(t: string) {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // sessionStorage unavailable: the token lives in memory for this page view
  }
}

const SUMMARY_ATTRS = [...KEY_ATTRS, 'x_handle', 'github', 'hn_user', 'wikidata', 'location'];

/** Attribute values side by side; values shared by all candidates are marked. */
function Candidate({
  ref_,
  view,
  shared,
  busy,
  onMergeInto,
  preds,
}: {
  ref_: EntityRef;
  view: EntityView | null | undefined;
  shared: Set<string>;
  busy: boolean;
  onMergeInto: () => void;
  preds: ReturnType<typeof usePredicates>['byId'];
}) {
  const attrs = view ? view.facts.filter((f) => !f.object && SUMMARY_ATTRS.includes(f.predicate)) : [];
  const edges = view ? view.facts.filter((f) => f.object).length + view.incoming.length : 0;
  return (
    <div className="candidate">
      <div>
        <KindTag kind={ref_.kind} />
        <div className="cname" style={{ marginTop: 6 }}>
          <Link to={entityPath(ref_.id)}>{displayName(ref_)}</Link>
        </div>
        <div className="cid">{ref_.id}</div>
      </div>
      {view === undefined ? (
        <div>
          <Skel h={12} />
          <Skel h={12} w="70%" style={{ marginTop: 8 }} />
          <Skel h={12} w="50%" style={{ marginTop: 8 }} />
        </div>
      ) : view === null ? (
        <p className="muted">Could not load this entity.</p>
      ) : (
        <dl>
          {attrs.map((f, i) => {
            const key = `${f.predicate}=${valueText(f.value)}`;
            return (
              <div key={i}>
                <dt>{labelOf(f.predicate, preds)}</dt>
                <dd className={shared.has(key) ? 'same' : ''} title={shared.has(key) ? 'Same on every candidate' : undefined}>
                  {valueText(f.value)} <SourceBadge fact={f} />
                </dd>
              </div>
            );
          })}
          <div>
            <dt>Connections</dt>
            <dd>{edges}</dd>
          </div>
          <div>
            <dt>Sources</dt>
            <dd>{[...new Set(view.facts.map((f) => f.layer))].join(', ') || '—'}</dd>
          </div>
          {view.merged_from.length > 0 && (
            <div>
              <dt>Merged</dt>
              <dd>{plural(view.merged_from.length, 'record')}</dd>
            </div>
          )}
        </dl>
      )}
      <div className="actions">
        <button type="button" className="btn sm primary" disabled={busy} onClick={onMergeInto}>
          Keep this, merge others in
        </button>
      </div>
    </div>
  );
}

function ReviewCard({ item, token, onDone }: { item: ReviewItem; token: string; onDone: (id: number, msg: string) => void }) {
  const client = useApi();
  const preds = usePredicates();
  const [views, setViews] = useState<Record<string, EntityView | null>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    const ctl = new AbortController();
    for (const c of item.candidates) {
      client
        .entity(c.id, { signal: ctl.signal })
        .then((r) => setViews((v) => ({ ...v, [c.id]: isRedirect(r) ? r.entity : r })))
        .catch((e: Error) => {
          if (e.name !== 'AbortError') setViews((v) => ({ ...v, [c.id]: null }));
        });
    }
    return () => ctl.abort();
  }, [client, item]);

  const shared = new Set<string>();
  const loaded = item.candidates.map((c) => views[c.id]).filter((v): v is EntityView => !!v);
  if (loaded.length > 1) {
    const sets = loaded.map((v) => new Set(v.facts.filter((f) => !f.object).map((f) => `${f.predicate}=${valueText(f.value)}`)));
    for (const k of sets[0]!) if (sets.every((s) => s.has(k))) shared.add(k);
  }

  const finish = (msg: string) => {
    setLeaving(true);
    setTimeout(() => onDone(item.id, msg), 280);
  };

  const mergeInto = async (into: string) => {
    setBusy(true);
    setError(null);
    try {
      // Merge every other un-merged candidate into `into`; the first call resolves the item.
      const others = item.candidates.filter((c) => c.id !== into);
      let first = true;
      for (const c of others) {
        await client.merge(token, { from: c.id, into, review_id: first ? item.id : null });
        first = false;
        clearCache(`entity:${c.id}`);
      }
      clearCache(`entity:${into}`);
      clearCache('home:');
      finish(`Merged ${others.map(displayName).join(', ')} into ${displayName(item.candidates.find((c) => c.id === into)!)}.`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const dismiss = async () => {
    setBusy(true);
    setError(null);
    try {
      await client.dismiss(token, item.id);
      finish(`Dismissed review #${item.id}.`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <article className={`review-item${leaving ? ' leaving' : ''}`} aria-label={`Review item ${item.id}`}>
      <header className="review-head">
        <span className="eyebrow">{item.kind.replace(/_/g, ' ')}</span>
        <span className="reason-text">{item.reason}</span>
        <span className="meta">
          #{item.id} · {relativeTime(item.created_at)}
        </span>
      </header>
      <div className="candidates">
        {item.candidates.map((c) => (
          <Candidate key={c.id} ref_={c} view={views[c.id]} shared={shared} busy={busy} preds={preds.byId} onMergeInto={() => mergeInto(c.id)} />
        ))}
        {item.candidates.length < item.entity_ids.length && (
          <div className="candidate muted">{item.entity_ids.length - item.candidates.length} candidate(s) no longer exist.</div>
        )}
      </div>
      <footer className="review-foot">
        {Array.isArray(item.evidence) && item.evidence.length > 0 && <span className="evidence">evidence: {JSON.stringify(item.evidence)}</span>}
        <span className="spacer" />
        {error && (
          <span role="alert" style={{ color: 'var(--accent)', fontSize: 12.5 }}>
            {error}
          </span>
        )}
        <button type="button" className="btn sm danger" disabled={busy} onClick={dismiss}>
          Not the same — dismiss
        </button>
      </footer>
    </article>
  );
}

export function ReviewPage() {
  useTitle('Review');
  const client = useApi();
  const [token, setToken] = useState(readToken);
  const [draft, setDraft] = useState('');
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    const ctl = new AbortController();
    setItems(null);
    setError(null);
    client
      .review(token, 'open', { signal: ctl.signal })
      .then(setItems)
      .catch((e: Error) => {
        if (e.name === 'AbortError') return;
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
          saveToken('');
          setToken('');
          setError(e.status === 401 ? 'That token was not accepted.' : 'That token is not an admin token.');
        } else setError(e.message);
      });
    return () => ctl.abort();
  }, [client, token]);

  if (!token) {
    return (
      <div className="wrap">
        <header style={{ paddingTop: 34 }}>
          <span className="eyebrow">Review queue · admin</span>
          <h1 className="page-title" style={{ marginTop: 8 }}>
            Possible duplicates
          </h1>
          <p className="lede" style={{ marginTop: 10 }}>
            The resolver sends ambiguous matches here. Reviewing needs the admin token; it is kept in this tab’s session storage only and sent as a Bearer header.
          </p>
        </header>
        <form
          className="token-form"
          onSubmit={(e) => {
            e.preventDefault();
            const t = draft.trim();
            if (!t) return;
            saveToken(t);
            setToken(t);
            setDraft('');
          }}
        >
          <input className="field" type="password" autoComplete="off" placeholder="Admin token" aria-label="Admin token" value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button type="submit" className="btn primary">
            Unlock
          </button>
        </form>
        {error && (
          <p className="notice" role="alert" style={{ marginTop: 14, maxWidth: 520 }}>
            {error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="wrap">
      <header style={{ paddingTop: 34, display: 'flex', alignItems: 'end', gap: 16, flexWrap: 'wrap' }}>
        <div>
          <span className="eyebrow">Review queue · admin</span>
          <h1 className="page-title" style={{ marginTop: 8 }}>
            Possible duplicates
          </h1>
        </div>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="muted" style={{ fontSize: 13 }}>
          {items ? `${plural(items.length, 'open item')}` : ''}
        </span>
        <button
          type="button"
          className="btn ghost sm"
          onClick={() => {
            saveToken('');
            setToken('');
            setItems(null);
          }}
        >
          Forget token
        </button>
      </header>
      {flash && (
        <p className="notice quiet fade-in" role="status" style={{ marginTop: 18 }}>
          {flash}
        </p>
      )}
      {error && <div className="error-box" style={{ marginTop: 18 }}>{error}</div>}
      {!items && !error && (
        <div className="review-item" aria-busy="true">
          <div className="review-head">
            <Skel w={260} h={16} />
          </div>
          <div className="candidates">
            {[0, 1].map((i) => (
              <div className="candidate" key={i}>
                <Skel w="60%" h={20} />
                <Skel w="90%" h={11} />
                <Skel w="80%" h={11} />
              </div>
            ))}
          </div>
        </div>
      )}
      {items && items.length === 0 && <p className="empty">Queue is clear. Nothing waiting for review.</p>}
      {items?.map((item) => (
        <ReviewCard
          key={item.id}
          item={item}
          token={token}
          onDone={(id, msg) => {
            setItems((list) => (list ? list.filter((x) => x.id !== id) : list));
            setFlash(msg);
          }}
        />
      ))}
      {items && items.length > 0 && (
        <p className="hint-line">
          <span>Merging moves the others’ facts onto the kept entity and redirects their ids. It can be undone with</span>
          <code className="mono">POST /v0/admin/split</code>
        </p>
      )}
    </div>
  );
}
