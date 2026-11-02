import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createClient } from '../src/api';
import { groupFacts, sourcesOf } from '../src/lib/facts';
import { ApiContext, clearCache } from '../src/lib/hooks';
import { EntityPage } from '../src/pages/Entity';
import { ODILE, PREDICATES, PRED_INDEX, QUILLWORK, mockFetch } from './fixtures';

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
    expect(m.calls.filter((c) => c.url.startsWith('/v0/entities/')).length).toBe(1);
  });

  it('shows a not-found page for unknown ids', async () => {
    renderEntity('/e/rbx%3Aperson%3Anope', {});
    expect(await screen.findByRole('heading', { name: 'Not in the graph' })).toBeTruthy();
  });
});
