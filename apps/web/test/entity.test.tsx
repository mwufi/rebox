import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import userEvent from '@testing-library/user-event';
import { createClient, type EntityView } from '../src/api';
import { edgeTotal, groupFacts, sourcesOf } from '../src/lib/facts';
import { buildGraph, degreeOf } from '../src/lib/graph';
import { ApiContext, clearCache } from '../src/lib/hooks';
import { EntityPage } from '../src/pages/Entity';
import { ODILE, PREDICATES, PRED_INDEX, QUILLWORK, fact, mockFetch, ref } from './fixtures';

/** S13: a hub whose view carries the first 12 of 30 incoming based_in edges, plus counts. */
const SF = ref('rbx:place:san-francisco', 'San Francisco');
const TENANTS = Array.from({ length: 30 }, (_, i) => ref(`rbx:company:t${String(i).padStart(2, '0')}`, `Tenant ${String(i).padStart(2, '0')}`));
const tenantFact = (c: ReturnType<typeof ref>) => fact({ predicate: 'based_in', subject: c, object: SF, source_ref: `t:${c.id}` });
const HUB: EntityView = {
  id: SF.id,
  kind: 'place',
  name: 'San Francisco',
  merged_from: [],
  facts: [fact({ predicate: 'name', value: 'San Francisco', subject: SF })],
  incoming: TENANTS.slice(0, 12).map(tenantFact),
  facts_counts: {},
  incoming_counts: { based_in: 30 },
};

describe('groupFacts', () => {
  it('splits key attributes, other attributes, outgoing and incoming edges', () => {
    const g = groupFacts(QUILLWORK, PRED_INDEX);
    expect(g.key.map((x) => x.predicate)).toEqual(['description', 'website', 'domain', 'yc_batch', 'founded_year']);
    expect(g.attributes).toEqual([]);
    expect(g.outgoing.map((x) => [x.predicate, x.label, x.facts.length])).toEqual([['based_in', 'Based in', 1]]);
    // Incoming groups read from this side and are ordered by size.
    expect(g.incoming.map((x) => [x.predicate, x.label, x.facts.length])).toEqual([
      ['founded', 'Founded by', 2],
      ['worked_at', 'People', 1],
    ]);
    // `name` is the title, never a row.
    expect(g.total).toBe(QUILLWORK.facts.length - 1 + QUILLWORK.incoming.length);
  });

  it('sorts edges current-first, then by most recent start', () => {
    const g = groupFacts(ODILE, PRED_INDEX);
    const worked = g.outgoing.find((x) => x.predicate === 'worked_at')!;
    expect(worked.label).toBe('Worked at');
    expect(worked.facts.map((f) => f.object!.name)).toEqual(['Quillwork', 'Tidewater Labs', 'Halyard Data']);
    expect(g.outgoing[0]!.predicate).toBe('worked_at');
    expect(g.attributes.map((x) => x.label)).toEqual(['Hacker News']);
    expect(g.key.map((x) => x.predicate)).toEqual(['headline']);
  });

  it('uses the view counts for group totals (S13) and falls back to the facts carried', () => {
    const g = groupFacts(HUB, PRED_INDEX);
    expect(g.incoming.map((x) => [x.predicate, x.facts.length, x.total])).toEqual([['based_in', 12, 30]]);
    expect(g.total).toBe(30);
    expect(edgeTotal(HUB)).toBe(30);
    // Views without counts (older servers, fixtures) count what they carry.
    expect(groupFacts(QUILLWORK, PRED_INDEX).incoming.map((x) => x.total)).toEqual([2, 1]);
    expect(edgeTotal(QUILLWORK)).toBe(4);
  });

  it('graph caps and degree count edges the view did not carry', () => {
    expect(degreeOf(HUB)).toBe(30);
    const g = buildGraph(HUB);
    const more = g.nodes.find((n) => n.more)!;
    expect(more.more).toMatchObject({ count: 18, predicate: 'based_in', dir: 'in' });
    expect(degreeOf(QUILLWORK)).toBe(3); // unique neighbors: Odile both founded and works there
  });

  it('counts facts per source layer', () => {
    expect(sourcesOf(QUILLWORK)).toEqual([
      { source: 'manual', layer: 'public/manual', n: 8 },
      { source: 'wikidata', layer: 'public/wikidata', n: 2 },
    ]);
  });
});

function Where() {
  const l = useLocation();
  return <span data-testid="where">{l.pathname}</span>;
}

function renderEntity(path: string, routes: Parameters<typeof mockFetch>[0]) {
  clearCache();
  const m = mockFetch({ 'GET /v0/predicates': () => PREDICATES, ...routes });
  const client = createClient({ fetch: m.fetch });
  render(
    <ApiContext.Provider value={client}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[path]}>
        <Routes>
          <Route path="/e/:id" element={<EntityPage />} />
        </Routes>
        <Where />
      </MemoryRouter>
    </ApiContext.Provider>,
  );
  return m;
}

describe('EntityPage', () => {
  it('renders grouped facts with linked refs, date ranges and source badges', async () => {
    renderEntity('/e/rbx%3Aperson%3Aodile', { 'GET /v0/entities/rbx%3Aperson%3Aodile': () => ODILE });
    expect(screen.getByLabelText('Loading entity')).toBeTruthy();
    expect(await screen.findByRole('heading', { level: 1, name: 'Odile Fernsby' })).toBeTruthy();

    const worked = screen.getByRole('region', { name: 'Worked at' });
    const links = within(worked).getAllByRole('link');
    expect(links.map((a) => a.textContent)).toEqual(['Quillwork', 'Tidewater Labs', 'Halyard Data']);
    expect(links[0]!.getAttribute('href')).toBe('/e/rbx%3Acompany%3Aquillwork');
    expect(within(worked).getByText('Mar 2017 – Aug 2021')).toBeTruthy();
    expect(within(worked).getByText('CEO')).toBeTruthy();
    // Each fact has a source badge naming its layer and source_ref.
    const badge = within(worked).getByLabelText('source public/wikidata, ref wikidata:Q1:P108');
    expect(badge.textContent).toContain('WD');

    // Incoming edges are grouped too.
    const known = screen.getByRole('region', { name: 'Known by' });
    expect(within(known).getByRole('link').textContent).toBe('Tomás Reyes-Oduya');

    // Merged-from note and graph link.
    expect(screen.getByRole('note').textContent).toContain('rbx:person:odile-hn');
    expect(screen.getAllByRole('link', { name: /graph/i })[0]!.getAttribute('href')).toBe('/e/rbx%3Aperson%3Aodile/graph');
    // Key attributes: headline is the description line, with its badge.
    expect(screen.getByText('CEO at Quillwork')).toBeTruthy();
  });

  it('follows merge redirects to the canonical URL', async () => {
    const m = renderEntity('/e/rbx%3Aperson%3Aodile-hn', {
      'GET /v0/entities/rbx%3Aperson%3Aodile-hn': () => ({ redirect: ODILE.id, entity: ODILE }),
      'GET /v0/entities/rbx%3Aperson%3Aodile': () => ODILE,
    });
    expect(await screen.findByRole('heading', { level: 1, name: 'Odile Fernsby' })).toBeTruthy();
    await waitFor(() => expect(decodeURIComponent(screen.getByTestId('where').textContent!)).toBe('/e/rbx:person:odile'));
    expect(screen.getByText(/was merged into this entity/).textContent).toContain('rbx:person:odile-hn');
    // The target view was primed from the redirect body: no second fetch.
    expect(m.calls.filter((c) => /^\/v0\/entities\/[^/]+$/.test(c.url)).length).toBe(1);
  });

  it('shows a hub group total and loads the rest from the edges endpoint (S13)', async () => {
    const enc = encodeURIComponent(SF.id);
    const m = renderEntity(`/e/${enc}`, {
      [`GET /v0/entities/${enc}`]: () => HUB,
      [`GET /v0/entities/${enc}/edges?predicate=based_in&dir=in&limit=112`]: () => ({
        edges: TENANTS.slice(0, 20).map(tenantFact),
        next: 'c1',
      }),
      [`GET /v0/entities/${enc}/edges?predicate=based_in&dir=in&limit=100&cursor=c1`]: () => ({
        edges: TENANTS.slice(20).map(tenantFact),
        next: null,
      }),
    });
    const group = await screen.findByRole('region', { name: 'Based here' });
    expect(within(group).getByText('30')).toBeTruthy();
    expect(within(group).getAllByRole('link')).toHaveLength(8);
    // Expanding fetches the first page (the 12 carried edges dedupe).
    await userEvent.click(within(group).getByRole('button', { name: '+22 more' }));
    await waitFor(() => expect(within(group).getAllByRole('link')).toHaveLength(20));
    await userEvent.click(within(group).getByRole('button', { name: '+10 more' }));
    await waitFor(() => expect(within(group).getAllByRole('link')).toHaveLength(30));
    expect(within(group).getAllByRole('link').map((a) => a.textContent)).toEqual(TENANTS.map((t) => t.name));
    expect(within(group).getByRole('button', { name: 'Show fewer' })).toBeTruthy();
    expect(m.calls.filter((c) => c.url.includes('/edges')).length).toBe(2);
    // The neighborhood thumbnail counts every connection.
    expect(screen.getByText(/30 connections/)).toBeTruthy();
  });

  it('shows a not-found page for unknown ids', async () => {
    renderEntity('/e/rbx%3Aperson%3Anope', {});
    expect(await screen.findByRole('heading', { name: 'Not in the graph' })).toBeTruthy();
  });
});

describe('EntityPage attributes sidebar (S19)', () => {
  const ACME = ref('rbx:company:acme', 'Acme');
  const preds = [
    ...PREDICATES,
    { id: 'milestone', subject_kinds: ['person', 'company'], object_kind: null, cardinality: 'many', meta: { label: 'Milestone', value_type: 'json', timeline: true } },
    { id: 'funding_round', subject_kinds: ['company'], object_kind: null, cardinality: 'one', meta: { label: 'Funding round', value_type: 'json' } },
  ];
  const ms = (i: number) =>
    fact({
      predicate: 'milestone',
      subject: ACME,
      value: { title: `Acme raised round ${i}`, kind: 'funding', url: `https://news.example/${i}` },
      valid_from: `2026-0${i}-01`,
      layer: 'public/exa_ai',
      source: 'exa_ai',
      source_ref: `exa_parse:a${i}:milestone`,
    });
  const VIEW: EntityView = {
    id: ACME.id,
    kind: 'company',
    name: 'Acme',
    merged_from: [],
    facts: [
      fact({ predicate: 'name', value: 'Acme', subject: ACME }),
      ms(1),
      ms(2),
      ms(3),
      fact({ predicate: 'funding_round', subject: ACME, value: { amount: '$5M', lead: 'Lux', url: 'https://lux.example/acme', note: null } }),
      fact({ predicate: 'x_followers', subject: ACME, value: 1234 }),
    ],
    incoming: [],
  };

  it('counts milestones with a link to the timeline and never shows raw JSON', async () => {
    const enc = encodeURIComponent(ACME.id);
    renderEntity(`/e/${enc}`, {
      'GET /v0/predicates': () => preds,
      [`GET /v0/entities/${enc}`]: () => VIEW,
      [`GET /v0/entities/${enc}/timeline*`]: () => ({ events: [] }),
    });
    const container = document.body;
    expect(await screen.findByRole('heading', { level: 1, name: 'Acme' })).toBeTruthy();
    const side = screen.getByRole('heading', { level: 3, name: 'Attributes' }).closest('.side-block') as HTMLElement;
    // No milestone row and no JSON anywhere in the sidebar.
    expect(within(side).queryByText('Milestone')).toBeNull();
    expect(side.textContent).not.toMatch(/[{}"]/);
    const link = within(side).getByRole('link', { name: '3 milestones — see Timeline' });
    expect(link.getAttribute('href')).toBe('#timeline');
    expect(container.querySelector('#timeline')).toBeTruthy();
    await userEvent.click(link); // scrolls (no-op in jsdom), never navigates
    // Other JSON-valued attributes read as compact key: value text.
    expect(within(side).getByText('amount: $5M · lead: Lux · url: lux.example/acme')).toBeTruthy();
    expect(within(side).getByText('X followers')).toBeTruthy();
    expect(container.textContent).not.toContain('{"title"');
  });
});
