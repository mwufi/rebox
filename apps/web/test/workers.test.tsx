import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createClient, type WorkerDetail, type WorkerSummary } from '../src/api';
import { ApiContext, clearCache } from '../src/lib/hooks';
import { EntityPage } from '../src/pages/Entity';
import { WorkerPage, WorkersPage, duration } from '../src/pages/Workers';
import { ODILE, PREDICATES, fact, mockFetch, ref } from './fixtures';

const HN = 'hn-connector-104233';
const PARSER = 'xbio-parser-551021';
const now = Date.now();
const hourIso = (h: number) => new Date(Math.floor(now / 3600e3) * 3600e3 - h * 3600e3).toISOString();

const RUN_OK = {
  id: 12, worker: HN, source: 'hn', kind: 'sync', status: 'ok' as const, started_at: hourIso(1), finished_at: hourIso(0),
  duration_s: 94.2, stats: { fetched: 100, claims: 420, written: 380, minted: 40, matched: 60, review: 1, rejected: 0 }, error: null,
};
const RUN_FAILED = {
  ...RUN_OK, id: 11, status: 'failed' as const, kind: 'replay', duration_s: 3.1, error: 'WriterError: writer returned 503',
};

const WORKERS: WorkerSummary[] = [
  { id: PARSER, kind: 'parser', source: 'x_parse', owner: 'rebox', trust: 0.6, status: 'active', description: null,
    last_seen_at: hourIso(2), claims_total: 50, claims_24h: 12, runs_ok: 3, runs_failed: 0,
    last_run: { ...RUN_OK, id: 9, worker: PARSER, kind: 'parse', stats: { api_calls: 20, cost_usd: 0.04 } } },
  { id: HN, kind: 'connector', source: 'hn', owner: 'rebox', trust: 0.8, status: 'active', description: null,
    last_seen_at: hourIso(0), claims_total: 5400, claims_24h: 380, runs_ok: 7, runs_failed: 1, last_run: RUN_FAILED },
];

const DETAIL: WorkerDetail = {
  ...WORKERS[1]!, layers: ['public/hn'], config: { seeds: ['frontpage'] }, created_at: '2026-11-01T00:00:00.000Z',
  runs: [RUN_OK, RUN_FAILED],
  throughput: Array.from({ length: 48 }, (_, i) => ({ t: hourIso(47 - i), n: i === 46 ? 380 : 0 })),
  predicates: [{ predicate: 'name', n: 200 }, { predicate: 'posted', n: 120 }],
  recent_facts: [
    fact({ predicate: 'posted', subject: ref('rbx:person:odile', 'Odile Fernsby'), object: ref('rbx:launch:quill', 'Show HN: Quillwork'),
      layer: 'public/hn', source: 'hn', source_ref: 'hn:story:1', worker: HN, support: 1, evidence: null }),
  ],
};

function renderAt(path: string, extra: Parameters<typeof mockFetch>[0] = {}) {
  clearCache();
  const m = mockFetch({
    'GET /v0/predicates': () => PREDICATES,
    'GET /v0/workers': () => WORKERS,
    'GET /v0/stats/timeseries*': () => ({ buckets: [{ t: hourIso(1), key: HN, n: 380 }, { t: hourIso(2), key: PARSER, n: 12 }] }),
    [`GET /v0/workers/${HN}`]: () => DETAIL,
    ...extra,
  });
  render(
    <ApiContext.Provider value={createClient({ fetch: m.fetch })}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[path]}>
        <Routes>
          <Route path="/workers" element={<WorkersPage />} />
          <Route path="/workers/:id" element={<WorkerPage />} />
          <Route path="/e/:id" element={<EntityPage />} />
        </Routes>
      </MemoryRouter>
    </ApiContext.Provider>,
  );
  return m;
}

describe('WorkersPage', () => {
  it('lists workers by kind with trust, 24h claims, a sparkline and the last run', async () => {
    const m = renderAt('/workers');
    const link = await screen.findByRole('link', { name: HN });
    expect(link.getAttribute('href')).toBe(`/workers/${HN}`);
    const rows = screen.getAllByRole('row').slice(1);
    // Connectors before parsers.
    expect(rows.map((r) => within(r).getByRole('rowheader').querySelector('a')!.textContent)).toEqual([HN, PARSER]);
    const hn = rows[0]!;
    expect(within(hn).getByText('connector')).toBeTruthy();
    expect(within(hn).getByText('0.80')).toBeTruthy();
    expect(within(hn).getByText('380')).toBeTruthy();
    expect(within(hn).getByText('failed').closest('[data-status]')!.getAttribute('title')).toBe('WriterError: writer returned 503');
    expect(within(hn).getByRole('img', { name: `${HN}: 380 claims in the last 48 hours` })).toBeTruthy();
    expect(within(rows[1]!).getByRole('img', { name: `${PARSER}: 12 claims in the last 48 hours` })).toBeTruthy();
    // Summary register.
    expect(screen.getByText('claims written, 24 h').previousElementSibling!.textContent).toBe('392');
    expect(screen.getByText('last run failed').previousElementSibling!.textContent).toBe('1');
    const ts = m.calls.find((c) => c.url.startsWith('/v0/stats/timeseries'))!;
    expect(ts.url).toMatch(/metric=claims&by=worker&bucket=hour&since=/);
  });
});

describe('WorkerPage', () => {
  it('shows runs with stats and durations, throughput, predicates and recent facts', async () => {
    renderAt(`/workers/${HN}`);
    expect(await screen.findByRole('heading', { level: 1, name: HN })).toBeTruthy();
    const runs = screen.getByRole('region', { name: 'Runs' });
    const rows = within(runs).getAllByRole('row').slice(1);
    expect(within(rows[0]!).getByText('1m 34s')).toBeTruthy();
    expect(within(rows[0]!).getByText('420')).toBeTruthy();
    expect(within(rows[1]!).getByText('failed')).toBeTruthy();
    expect(within(runs).getByText('WriterError: writer returned 503')).toBeTruthy();
    const tp = screen.getByRole('region', { name: 'Throughput' });
    expect(within(tp).getByText('380 claims written, last 48 h')).toBeTruthy();
    // Hovering a column shows its value.
    const col = tp.querySelectorAll('path.col');
    expect(col).toHaveLength(1);
    fireEvent.mouseEnter(col[0]!.parentElement!);
    expect(within(tp).getByText('380').tagName).toBe('B');
    expect(screen.getByRole('list', { name: 'Claims by predicate' }).textContent).toContain('Posted');
    const recent = screen.getByRole('region', { name: 'Recent facts' });
    expect(within(recent).getByRole('link', { name: 'Show HN: Quillwork' }).getAttribute('href')).toBe('/e/rbx%3Alaunch%3Aquill');
    expect(screen.getByText('{"seeds":["frontpage"]}')).toBeTruthy();
  });

  it('404s for unknown workers', async () => {
    renderAt('/workers/nope-connector-000000');
    expect(await screen.findByRole('heading', { name: 'Not in the graph' })).toBeTruthy();
  });

  it('formats durations', () => {
    expect([duration(null), duration(3.14), duration(42), duration(94.2), duration(7322)]).toEqual(['—', '3.1s', '42s', '1m 34s', '2h 2m']);
  });
});

describe('fact provenance on the entity page', () => {
  it('shows support counts, evidence links with authors, and the worker', async () => {
    const quill = ref('rbx:company:quillwork', 'Quillwork');
    const view = {
      ...ODILE,
      facts: [
        ...ODILE.facts.filter((f) => f.predicate !== 'worked_at'),
        fact({ predicate: 'worked_at', object: quill, value: { title: 'CEO' }, worker: PARSER, support: 2,
          evidence: [{ url: 'https://x.com/odile/status/9', author: 'odile', quote: 'CEO @quillwork' }] }),
      ],
    };
    renderAt('/e/rbx%3Aperson%3Aodile', { 'GET /v0/entities/rbx%3Aperson%3Aodile': () => view });
    const worked = await screen.findByRole('region', { name: 'Worked at' });
    expect(within(worked).getByText('2 sources')).toBeTruthy();
    const src = within(worked).getByRole('link', { name: /Source ↗/ });
    expect(src.getAttribute('href')).toBe('https://x.com/odile/status/9');
    expect(src.getAttribute('target')).toBe('_blank');
    expect(src.getAttribute('rel')).toContain('noopener');
    expect(src.textContent).toBe('Source ↗ · odile');
    expect(src.getAttribute('title')).toContain('“CEO @quillwork”');
    expect(within(worked).getByRole('link', { name: PARSER }).getAttribute('href')).toBe(`/workers/${PARSER}`);
    // Facts without provenance fields render as before.
    expect(screen.queryAllByText(/sources$/)).toHaveLength(1);
  });
});
