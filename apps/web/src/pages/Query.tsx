/** /query — builder for the §6 filter list; the state lives in the URL so queries are shareable. */
import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import type { Predicate, QueryResult } from '../api';
import { CopyButton, EntityLink, Icon, KindTag, SkelRows } from '../components/bits';
import { EntityPicker } from '../components/EntityPicker';
import { KINDS, kindLabel } from '../lib/format';
import { useApi, useAsync, usePredicates, useTitle } from '../lib/hooks';
import {
  ATTR_OPS,
  attrPredicatesFor,
  edgePredicatesFor,
  newRow,
  paramsToState,
  rowComplete,
  stateToParams,
  stateToQuery,
  type BuilderState,
  type Row,
} from '../lib/queryState';
import { labelOf } from '../lib/facts';

const OP_LABEL: Record<string, string> = { eq: 'equals', contains: 'contains', gte: 'at least', lte: 'at most' };

function RowEditor({
  row,
  index,
  find,
  preds,
  onChange,
  onRemove,
}: {
  row: Row;
  index: number;
  find: string;
  preds: Predicate[];
  onChange: (r: Row) => void;
  onRemove: () => void;
}) {
  const byId = new Map(preds.map((p) => [p.id, p]));
  let body: React.ReactNode;
  if (row.type === 'name') {
    body = (
      <>
        <span className="tag">name contains</span>
        <input className="field" value={row.text} placeholder="e.g. chen" aria-label={`Filter ${index + 1}: name contains`} onChange={(e) => onChange({ ...row, text: e.target.value })} />
      </>
    );
  } else if (row.type === 'edge') {
    const options = edgePredicatesFor(preds, find);
    const p = byId.get(row.predicate);
    const otherKind = p ? (row.dir === 'out' ? p.object_kind : p.subject_kinds[0]) : undefined;
    body = (
      <>
        <span className="tag">relation</span>
        <select
          className="field"
          aria-label={`Filter ${index + 1}: relation`}
          value={`${row.dir}:${row.predicate}`}
          onChange={(e) => {
            const [dir, predicate] = e.target.value.split(':') as ['out' | 'in', string];
            onChange({ ...row, dir, predicate, to: '' });
          }}
        >
          {options.map(({ p: o, dir }) => (
            <option key={`${dir}:${o.id}`} value={`${dir}:${o.id}`}>
              {dir === 'out' ? `${labelOf(o.id, byId)} →  ${o.object_kind}` : `← ${labelOf(o.id, byId).toLowerCase()} by ${o.subject_kinds.join('/')}`}
            </option>
          ))}
        </select>
        <EntityPicker
          value={row.to}
          kind={otherKind ?? undefined}
          label={`Filter ${index + 1}: ${row.dir === 'out' ? 'target' : 'source'}`}
          placeholder={`any ${otherKind ?? 'entity'}`}
          onChange={(to) => onChange({ ...row, to })}
        />
        <span className="during">
          during
          <input className="field" type="text" inputMode="numeric" placeholder="from YYYY-MM-DD" value={row.from} aria-label={`Filter ${index + 1}: during from`} onChange={(e) => onChange({ ...row, from: e.target.value })} />
          –
          <input className="field" type="text" inputMode="numeric" placeholder="to YYYY-MM-DD" value={row.until} aria-label={`Filter ${index + 1}: during to`} onChange={(e) => onChange({ ...row, until: e.target.value })} />
        </span>
      </>
    );
  } else {
    const options = attrPredicatesFor(preds, find);
    const p = byId.get(row.predicate);
    const numeric = p?.meta.value_type === 'int' || p?.meta.value_type === 'number';
    const ops = numeric ? ATTR_OPS.filter((o) => o !== 'contains') : ATTR_OPS;
    body = (
      <>
        <span className="tag">attribute</span>
        <select className="field" aria-label={`Filter ${index + 1}: attribute`} value={row.predicate} onChange={(e) => onChange({ ...row, predicate: e.target.value })}>
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {labelOf(o.id, byId)}
            </option>
          ))}
        </select>
        <select className="field narrow" aria-label={`Filter ${index + 1}: operator`} value={row.op} onChange={(e) => onChange({ ...row, op: e.target.value as Row extends { op: infer O } ? O : never })}>
          {ops.map((o) => (
            <option key={o} value={o}>
              {OP_LABEL[o]}
            </option>
          ))}
        </select>
        <input
          className="field"
          value={row.value}
          inputMode={numeric ? 'numeric' : undefined}
          placeholder={numeric ? 'number' : 'value'}
          aria-label={`Filter ${index + 1}: value`}
          onChange={(e) => onChange({ ...row, value: e.target.value })}
        />
      </>
    );
  }
  return (
    <li className="builder-row">
      <span className="conj">{index === 0 ? 'where' : 'and'}</span>
      <div className="inputs">{body}</div>
      <button type="button" className="copy-btn" aria-label={`Remove filter ${index + 1}`} onClick={onRemove}>
        <Icon name="x" size={12} />
      </button>
    </li>
  );
}

function Results({ data, loading, error, onMore, moreLoading }: { data?: QueryResult; loading: boolean; error?: Error; onMore?: () => void; moreLoading: boolean }) {
  if (error) return <div className="error-box" role="alert">{error.message}</div>;
  if (loading && !data) return <SkelRows n={7} h={58} className="facts" />;
  if (!data) return null;
  if (data.results.length === 0) return <p className="empty">No matches. Loosen a filter, or check the date range.</p>;
  return (
    <>
      <ul className="result-list fade-in">
        {data.results.map((r) => (
          <li key={r.id}>
            <div className="result-row" style={{ cursor: 'default' }}>
              <EntityLink entity={r} />
              <span className="id">{r.id}</span>
              {r.reasons.length > 0 && (
                <span className="reason-list">
                  {r.reasons.map((reason, i) => (
                    <span className="reason" key={i}>
                      {reason}
                    </span>
                  ))}
                </span>
              )}
            </div>
          </li>
        ))}
      </ul>
      {onMore && (
        <button type="button" className="btn more-btn" onClick={onMore} disabled={moreLoading}>
          {moreLoading ? 'Loading…' : 'Load more'}
        </button>
      )}
    </>
  );
}

export function QueryPage() {
  useTitle('Query');
  const [params, setParams] = useSearchParams();
  const state = useMemo(() => paramsToState(params), [params]);
  const preds = usePredicates();
  const client = useApi();

  const setState = (s: BuilderState) => setParams(stateToParams(s), { replace: true });
  const update = (i: number, r: Row) => setState({ ...state, rows: state.rows.map((x, j) => (j === i ? r : x)) });
  const remove = (i: number) => setState({ ...state, rows: state.rows.filter((_, j) => j !== i) });
  const add = (type: Row['type']) => setState({ ...state, rows: [...state.rows, newRow(type, preds.list, state.find)] });

  const body = useMemo(() => stateToQuery(state, preds.byId), [state, preds.byId]);
  const bodyKey = preds.loading ? null : `query:${JSON.stringify(body)}`;
  const r = useAsync(bodyKey, (signal) => client.query(body, { signal }), { cache: false });

  // "Load more" appends pages for the current body.
  const [extra, setExtra] = useState<{ key: string; pages: QueryResult[]; loading: boolean }>({ key: '', pages: [], loading: false });
  const pages = extra.key === bodyKey ? extra.pages : [];
  const lastNext = pages.length ? pages[pages.length - 1]!.next : r.data?.next;
  const merged: QueryResult | undefined = r.data ? { results: [...r.data.results, ...pages.flatMap((p) => p.results)], next: lastNext ?? null } : undefined;
  const more = async () => {
    if (!lastNext || !bodyKey) return;
    setExtra({ key: bodyKey, pages, loading: true });
    try {
      const page = await client.query({ ...body, cursor: lastNext });
      setExtra({ key: bodyKey, pages: [...pages, page], loading: false });
    } catch {
      setExtra({ key: bodyKey, pages, loading: false });
    }
  };

  const incomplete = state.rows.filter((x) => !rowComplete(x)).length;
  const shareUrl = typeof window !== 'undefined' ? window.location.href : '';
  const curl = `curl -s ${typeof window !== 'undefined' ? window.location.origin : ''}/v0/query \\\n  -H 'content-type: application/json' \\\n  -d '${JSON.stringify(body)}'`;

  return (
    <div className="wrap">
      <header style={{ paddingTop: 34 }}>
        <span className="eyebrow">Query</span>
        <h1 className="page-title" style={{ marginTop: 8 }}>
          Ask the graph
        </h1>
        <p className="lede" style={{ marginTop: 10 }}>
          Combine filters on names, relations (with dates) and attributes. Every filter must hold; each result says why it matched. The URL is the query — share it.
        </p>
      </header>

      <div className="query-layout">
        <div className="builder">
          <div className="builder-find">
            <span>Find</span>
            <select
              className="field"
              aria-label="Find kind"
              value={state.find}
              onChange={(e) => setState({ find: e.target.value, rows: state.rows.filter((x) => x.type === 'name') })}
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {kindLabel(k)}
                </option>
              ))}
            </select>
          </div>
          <ul className="builder-rows">
            {state.rows.map((row, i) => (
              <RowEditor key={i} row={row} index={i} find={state.find} preds={preds.list} onChange={(r2) => update(i, r2)} onRemove={() => remove(i)} />
            ))}
          </ul>
          <div className="builder-add">
            <span className="muted" style={{ fontSize: 12, marginRight: 4 }}>
              Add
            </span>
            <button type="button" className="btn sm" onClick={() => add('name')}>
              <Icon name="plus" size={11} /> Name
            </button>
            <button type="button" className="btn sm" onClick={() => add('edge')} disabled={!edgePredicatesFor(preds.list, state.find).length}>
              <Icon name="plus" size={11} /> Relation
            </button>
            <button type="button" className="btn sm" onClick={() => add('attr')} disabled={!attrPredicatesFor(preds.list, state.find).length}>
              <Icon name="plus" size={11} /> Attribute
            </button>
          </div>
          <div className="builder-foot">
            <span className="muted" style={{ fontSize: 12 }}>
              {incomplete ? `${incomplete} incomplete filter${incomplete > 1 ? 's' : ''} ignored` : 'Updates as you type'}
            </span>
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 12 }}>
              Link
            </span>
            <CopyButton text={shareUrl} label="Copy link to this query" />
            <span className="muted" style={{ fontSize: 12 }}>
              curl
            </span>
            <CopyButton text={curl} label="Copy as curl" />
            <Link to="/query" className="btn ghost sm">
              Reset
            </Link>
          </div>
          <pre className="query-json" aria-label="Request body">
            {JSON.stringify(body, null, 2)}
          </pre>
        </div>

        <section aria-label="Results" aria-live="polite">
          <div className="section-head">
            <h2>
              <KindTag kind={state.find} plural />
            </h2>
            {merged && (
              <span className="count">
                {merged.results.length}
                {merged.next ? '+' : ''} result{merged.results.length === 1 ? '' : 's'}
              </span>
            )}
            {r.loading && merged && <span className="aside">updating…</span>}
          </div>
          <Results data={merged} loading={r.loading || preds.loading} error={r.error} onMore={merged?.next ? more : undefined} moreLoading={extra.loading} />
        </section>
      </div>
    </div>
  );
}
