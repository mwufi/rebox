/**
 * /workers and /workers/:id — who writes the graph (CONTRACTS §12, §14): every worker's kind,
 * trust, health and throughput; one worker's runs, output and recent facts.
 */
import { useMemo } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ApiError, type Run, type WorkerDetail, type WorkerSummary } from '../api';
import { EntityLink, FactProvenance, Skel, SkelRows, SourceBadge, workerPath } from '../components/bits';
import { Columns, HBars, Sparkline } from '../components/charts';
import { labelOf } from '../lib/facts';
import { edgeProps, plural, relativeTime, valueText } from '../lib/format';
import { useApi, useAsync, usePredicates, useTitle } from '../lib/hooks';
import { NotFound } from './NotFound';

const KIND_ORDER = ['connector', 'spider', 'parser', 'enricher', 'external'];

export function duration(s: number | null): string {
  if (s === null) return '—';
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

function RunStatus({ run }: { run: Pick<Run, 'status' | 'error'> | null }) {
  if (!run) return <span className="muted">—</span>;
  const label = run.status === 'ok' ? 'ok' : run.status === 'failed' ? 'failed' : 'running';
  return (
    <span className="run-status" data-status={run.status} title={run.error ?? undefined}>
      <i aria-hidden="true" />
      {label}
    </span>
  );
}

function Trust({ value }: { value: number }) {
  return (
    <span className="trust" title={`trust ${value.toFixed(2)}: within a layer scope, higher trust wins the fold`}>
      <span className="track" aria-hidden="true">
        <i style={{ width: `${Math.round(value * 100)}%` }} />
      </span>
      <span className="num mono">{value.toFixed(2)}</span>
    </span>
  );
}

/** Claims per hour for the last 48 h, for every listed worker (one timeseries call). */
function useHourly(): Map<string, number[]> | undefined {
  const client = useApi();
  const since = useMemo(() => new Date(Math.floor(Date.now() / 3600e3) * 3600e3 - 47 * 3600e3).toISOString(), []);
  const r = useAsync(`ts:claims:worker:${since}`, (signal) =>
    client.timeseries({ metric: 'claims', by: 'worker', bucket: 'hour', since, signal }),
  );
  return useMemo(() => {
    if (!r.data) return undefined;
    const start = Date.parse(since);
    const out = new Map<string, number[]>();
    for (const b of r.data.buckets) {
      const i = Math.round((Date.parse(b.t) - start) / 3600e3);
      if (i < 0 || i > 47) continue;
      const arr = out.get(b.key) ?? Array.from({ length: 48 }, () => 0);
      arr[i] = (arr[i] ?? 0) + b.n;
      out.set(b.key, arr);
    }
    return out;
  }, [r.data, since]);
}

export function WorkersPage() {
  const client = useApi();
  useTitle('Workers');
  const r = useAsync('workers', (signal) => client.workers({ signal }));
  const hourly = useHourly();
  const workers = useMemo(
    () =>
      [...(r.data ?? [])].sort(
        (a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || b.claims_total - a.claims_total || a.id.localeCompare(b.id),
      ),
    [r.data],
  );
  const totals = useMemo(
    () => ({
      claims: workers.reduce((n, w) => n + w.claims_24h, 0),
      failing: workers.filter((w) => w.last_run?.status === 'failed').length,
    }),
    [workers],
  );

  return (
    <div className="wrap workers-page">
      <header className="page-head">
        <p className="eyebrow">Provenance</p>
        <h1 className="page-title">Workers</h1>
        <p className="lede">
          Every claim in Rebox is written by a named worker inside a run: connectors and spiders that fetch public sources,
          parsers that read text, and outside contributors. Trust decides ties between them.
        </p>
      </header>

      {r.error && <p className="empty">Could not load workers: {r.error.message}</p>}
      {r.data && (
        <div className="register workers-register" aria-label="Summary">
          <div>
            <span className="n num">{workers.length}</span>
            <span className="l">workers</span>
          </div>
          <div>
            <span className="n num">{totals.claims.toLocaleString('en-US')}</span>
            <span className="l">claims written, 24 h</span>
          </div>
          <div>
            <span className="n num">{totals.failing}</span>
            <span className="l">last run failed</span>
          </div>
        </div>
      )}

      <div className="table-scroll">
        <table className="data-table workers-table">
          <thead>
            <tr>
              <th scope="col">Worker</th>
              <th scope="col">Kind</th>
              <th scope="col">Trust</th>
              <th scope="col">Last seen</th>
              <th scope="col" className="r">
                Claims 24 h
              </th>
              <th scope="col">Claims / hour, 48 h</th>
              <th scope="col">Last run</th>
            </tr>
          </thead>
          <tbody>
            {!r.data &&
              !r.error &&
              Array.from({ length: 4 }, (_, i) => (
                <tr key={i} aria-busy="true">
                  <td colSpan={7}>
                    <Skel w={`${40 + i * 12}%`} h={13} />
                  </td>
                </tr>
              ))}
            {workers.map((w) => (
              <WorkerRow key={w.id} w={w} series={hourly?.get(w.id)} />
            ))}
          </tbody>
        </table>
      </div>
      {r.data && workers.length === 0 && <p className="empty">No workers have written yet. They register on their first sync.</p>}
    </div>
  );
}

function WorkerRow({ w, series }: { w: WorkerSummary; series: number[] | undefined }) {
  const values = series ?? Array.from({ length: 48 }, () => 0);
  return (
    <tr data-status={w.status}>
      <th scope="row">
        <Link to={workerPath(w.id)} className="worker-name mono">
          {w.id}
        </Link>
        <span className="sub">
          {w.source}
          {w.owner !== 'rebox' && <> · by {w.owner}</>}
          {w.status !== 'active' && <span className="badge-status"> {w.status}</span>}
        </span>
      </th>
      <td>
        <span className="wkind" data-kind={w.kind}>
          {w.kind}
        </span>
      </td>
      <td>
        <Trust value={w.trust} />
      </td>
      <td className="mono muted nowrap" title={w.last_seen_at ?? undefined}>
        {w.last_seen_at ? relativeTime(w.last_seen_at) : 'never'}
      </td>
      <td className="r num mono">{w.claims_24h.toLocaleString('en-US')}</td>
      <td>
        <Sparkline values={values} label={`${w.id}: ${values.reduce((a, b) => a + b, 0)} claims in the last 48 hours`} />
      </td>
      <td className="nowrap">
        <RunStatus run={w.last_run} />
        {w.last_run && (
          <span className="mono muted small">
            {' '}
            {w.last_run.kind} · {relativeTime(w.last_run.started_at)}
          </span>
        )}
      </td>
    </tr>
  );
}

// ── detail ────────────────────────────────────────────────────────────────

const STAT_COLS = ['fetched', 'claims', 'written', 'minted', 'matched', 'review', 'rejected'] as const;

function RunsTable({ runs }: { runs: Run[] }) {
  if (runs.length === 0) return <p className="empty">No runs yet.</p>;
  const extra = [...new Set(runs.flatMap((r) => Object.keys(r.stats)).filter((k) => k === 'api_calls' || k === 'cost_usd'))];
  return (
    <div className="table-scroll">
      <table className="data-table runs-table">
        <thead>
          <tr>
            <th scope="col">Run</th>
            <th scope="col">Status</th>
            <th scope="col">Started</th>
            <th scope="col" className="r">
              Duration
            </th>
            {STAT_COLS.map((k) => (
              <th scope="col" className="r" key={k}>
                {k}
              </th>
            ))}
            {extra.map((k) => (
              <th scope="col" className="r" key={k}>
                {k === 'cost_usd' ? 'cost' : 'API calls'}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} data-status={r.status}>
              <th scope="row" className="mono">
                #{r.id} <span className="muted">{r.kind}</span>
              </th>
              <td>
                <RunStatus run={r} />
              </td>
              <td className="mono muted nowrap" title={r.started_at}>
                {relativeTime(r.started_at)}
              </td>
              <td className="r num mono">{r.status === 'running' ? 'running' : duration(r.duration_s)}</td>
              {STAT_COLS.map((k) => (
                <td className="r num mono" key={k}>
                  {typeof r.stats[k] === 'number' ? (r.stats[k] as number).toLocaleString('en-US') : '—'}
                </td>
              ))}
              {extra.map((k) => (
                <td className="r num mono" key={k}>
                  {typeof r.stats[k] === 'number'
                    ? k === 'cost_usd'
                      ? `$${(r.stats[k] as number).toFixed(2)}`
                      : (r.stats[k] as number).toLocaleString('en-US')
                    : '—'}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {runs.some((r) => r.error) && (
        <ul className="run-errors">
          {runs
            .filter((r) => r.error)
            .slice(0, 5)
            .map((r) => (
              <li key={r.id}>
                <span className="mono">#{r.id}</span> <code>{r.error}</code>
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}

function hourLabel(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', timeZone: 'UTC' }) + ' UTC';
}

function WorkerBody({ w }: { w: WorkerDetail }) {
  const preds = usePredicates();
  const tp = w.throughput.map((b) => ({ key: b.t, label: hourLabel(b.t), value: b.n }));
  const tpTotal = tp.reduce((a, b) => a + b.value, 0);
  const config = Object.keys(w.config ?? {}).length ? JSON.stringify(w.config) : null;
  return (
    <>
      <header className="entity-head fade-in">
        <div className="crumbs">
          <Link to="/workers" className="eyebrow">
            ← Workers
          </Link>
          <span className="wkind" data-kind={w.kind}>
            {w.kind}
          </span>
        </div>
        <h1 className="entity-name worker-title mono">{w.id}</h1>
        {w.description && <p className="entity-desc">{w.description}</p>}
      </header>

      <dl className="keyattrs" aria-label="Worker attributes">
        <div>
          <dt className="eyebrow">Trust</dt>
          <dd>
            <Trust value={w.trust} />
          </dd>
        </div>
        <div>
          <dt className="eyebrow">Status</dt>
          <dd>{w.status}</dd>
        </div>
        <div>
          <dt className="eyebrow">Last seen</dt>
          <dd className="mono" title={w.last_seen_at ?? undefined}>
            {w.last_seen_at ? relativeTime(w.last_seen_at) : 'never'}
          </dd>
        </div>
        <div>
          <dt className="eyebrow">Claims</dt>
          <dd className="num">
            {w.claims_total.toLocaleString('en-US')} <span className="muted small">({w.claims_24h.toLocaleString('en-US')} in 24 h)</span>
          </dd>
        </div>
        <div>
          <dt className="eyebrow">Runs</dt>
          <dd className="num">
            {w.runs_ok} ok · {w.runs_failed} failed
          </dd>
        </div>
      </dl>

      <div className="entity-body">
        <div>
          <section className="edge-group" aria-label="Throughput">
            <div className="section-head">
              <h3>Throughput</h3>
              <span className="count">{plural(tpTotal, 'claim')} written, last 48 h</span>
            </div>
            <Columns data={tp} unit="claims" ticks={(i) => (i % 12 === 0 ? hourLabel(w.throughput[i]!.t) : null)} />
          </section>

          <section className="edge-group" aria-label="Runs">
            <div className="section-head">
              <h3>Runs</h3>
              <span className="count">{w.runs.length}</span>
            </div>
            <RunsTable runs={w.runs} />
          </section>

          <section className="edge-group" aria-label="Recent facts">
            <div className="section-head">
              <h3>Recent facts</h3>
              <span className="count">{w.recent_facts.length}</span>
            </div>
            {w.recent_facts.length === 0 ? (
              <p className="empty">Nothing written yet.</p>
            ) : (
              <ul className="facts">
                {w.recent_facts.map((f, i) => (
                  <li key={`${f.subject.id}|${f.predicate}|${f.source_ref}|${i}`}>
                    <span className="main">
                      <EntityLink entity={f.subject} />
                      <span className="props">{labelOf(f.predicate, preds.byId).toLowerCase()}</span>
                      {f.object ? (
                        <EntityLink entity={f.object} />
                      ) : (
                        <span className="fact-value">{valueText(f.value)}</span>
                      )}
                      {f.object && edgeProps(f.value) && <span className="props">{edgeProps(f.value)}</span>}
                      <FactProvenance fact={{ evidence: f.evidence }} />
                    </span>
                    <span className="when">{relativeTime(f.updated_at)}</span>
                    <SourceBadge fact={f} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
        <aside>
          <div className="side-block">
            <div className="section-head">
              <h3>Claims by predicate</h3>
            </div>
            {w.predicates.length ? (
              <HBars
                label="Claims by predicate"
                rows={w.predicates.slice(0, 16).map((p) => ({ key: p.predicate, label: labelOf(p.predicate, preds.byId), value: p.n }))}
              />
            ) : (
              <p className="empty">None yet.</p>
            )}
          </div>
          <div className="side-block">
            <div className="section-head">
              <h3>Identity</h3>
            </div>
            <dl className="attr-list">
              <div>
                <dt>Source</dt>
                <dd className="mono">{w.source}</dd>
              </div>
              <div>
                <dt>Owner</dt>
                <dd className="mono">{w.owner}</dd>
              </div>
              <div>
                <dt>Layers</dt>
                <dd className="mono">
                  {w.layers.map((l) => (
                    <span key={l}>{l}</span>
                  ))}
                </dd>
              </div>
              {config && (
                <div>
                  <dt>Config</dt>
                  <dd>
                    <code className="config">{config}</code>
                  </dd>
                </div>
              )}
              <div>
                <dt>Since</dt>
                <dd className="mono">{w.created_at.slice(0, 10)}</dd>
              </div>
            </dl>
          </div>
        </aside>
      </div>
    </>
  );
}

export function WorkerPage() {
  const { id = '' } = useParams();
  const client = useApi();
  const r = useAsync(`worker:${id}`, (signal) => client.worker(id, { signal }));
  useTitle(r.data ? r.data.id : 'Worker');
  if (r.error instanceof ApiError && r.error.status === 404) return <NotFound what={`No worker with id ${id}.`} />;
  return (
    <div className="wrap">
      {r.error && <p className="empty">Could not load: {r.error.message}</p>}
      {!r.data && !r.error && (
        <div aria-busy="true" aria-label="Loading worker">
          <header className="entity-head">
            <Skel w={120} h={11} />
            <Skel w="min(420px, 80%)" h={40} style={{ marginTop: 8 }} />
          </header>
          <SkelRows n={6} />
        </div>
      )}
      {r.data && <WorkerBody w={r.data} />}
    </div>
  );
}
