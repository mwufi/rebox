import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createClient } from '../src/api';
import { ApiContext, clearCache } from '../src/lib/hooks';
import {
  decodeRow,
  edgePredicatesFor,
  attrPredicatesFor,
  encodeRow,
  paramsToState,
  stateToParams,
  stateToQuery,
  type BuilderState,
} from '../src/lib/queryState';
import { QueryPage } from '../src/pages/Query';
import { PREDICATES, PRED_INDEX, mockFetch } from './fixtures';

const STATE: BuilderState = {
  find: 'person',
  rows: [
    { type: 'name', text: 'chen ~ & co/50%' },
    { type: 'edge', predicate: 'worked_at', dir: 'out', to: 'rbx:company:halyard', from: '2018-01-01', until: '2021-12-31' },
    { type: 'edge', predicate: 'founded', dir: 'out', to: '', from: '', until: '' },
    { type: 'attr', predicate: 'x_followers', op: 'gte', value: '1000' },
  ],
};

describe('query builder state', () => {
  it('round-trips through URL params', () => {
    const p = stateToParams(STATE);
    const url = `/query?${p.toString()}`;
    const back = paramsToState(new URL(url, 'http://x').searchParams);
    expect(back).toEqual(STATE);
    // Short, readable rows: trailing empty parts are dropped.
    expect(p.getAll('w')[2]).toBe('edge~founded~out');
    expect(decodeRow(encodeRow(STATE.rows[0]!))).toEqual(STATE.rows[0]);
  });

  it('ignores unknown rows and defaults find', () => {
    const s = paramsToState(new URLSearchParams('w=bogus~x&w=name~ada'));
    expect(s).toEqual({ find: 'person', rows: [{ type: 'name', text: 'ada' }] });
  });

  it('compiles to the §6 query body', () => {
    expect(stateToQuery(STATE, PRED_INDEX)).toEqual({
      find: 'person',
      where: [
        { name: 'chen ~ & co/50%' },
        { edge: { predicate: 'worked_at', dir: 'out', to: 'rbx:company:halyard', during: { from: '2018-01-01', to: '2021-12-31' } } },
        { edge: { predicate: 'founded', dir: 'out' } },
        { attr: { predicate: 'x_followers', gte: 1000 } },
      ],
      limit: 50,
    });
  });

  it('skips incomplete rows and keeps text attributes as strings', () => {
    const q = stateToQuery(
      { find: 'company', rows: [{ type: 'name', text: '  ' }, { type: 'attr', predicate: 'yc_batch', op: 'eq', value: 'W24' }, { type: 'attr', predicate: 'founded_year', op: 'eq', value: '' }] },
      PRED_INDEX,
    );
    expect(q.where).toEqual([{ attr: { predicate: 'yc_batch', eq: 'W24' } }]);
  });

  it('offers only public, filterable predicates that apply to the kind', () => {
    expect(edgePredicatesFor(PREDICATES, 'person').map((x) => `${x.dir}:${x.p.id}`)).toEqual(['out:worked_at', 'out:founded', 'out:based_in']);
    expect(edgePredicatesFor(PREDICATES, 'company').map((x) => `${x.dir}:${x.p.id}`)).toEqual(['in:worked_at', 'in:founded', 'out:based_in']);
    expect(attrPredicatesFor(PREDICATES, 'company').map((p) => p.id)).toEqual(['yc_batch', 'founded_year', 'x_followers']);
  });
});

function Where() {
  const l = useLocation();
  return <output data-testid="url">{l.pathname + l.search}</output>;
}

describe('QueryPage', () => {
  it('runs the query from the URL, shows reasons, and writes edits back to the URL', async () => {
    clearCache();
    const m = mockFetch({
      'GET /v0/predicates': () => PREDICATES,
      'POST /v0/query': (body) => ({
        results: [{ id: 'rbx:company:quillwork', kind: 'company', name: 'Quillwork', reasons: [`yc_batch ${JSON.stringify(body)}`.slice(0, 20)] }],
        next: null,
      }),
    });
    const start = `/query?${stateToParams({ find: 'company', rows: [{ type: 'attr', predicate: 'yc_batch', op: 'eq', value: 'W24' }] })}`;
    render(
      <ApiContext.Provider value={createClient({ fetch: m.fetch })}>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[start]}>
          <Routes>
            <Route path="/query" element={<QueryPage />} />
          </Routes>
          <Where />
        </MemoryRouter>
      </ApiContext.Provider>,
    );
    expect(await screen.findByRole('link', { name: 'Quillwork' })).toBeTruthy();
    expect(screen.getByText(/^yc_batch/)).toBeTruthy();
    const first = m.calls.find((c) => c.method === 'POST')!;
    expect(first.body).toEqual({ find: 'company', where: [{ attr: { predicate: 'yc_batch', eq: 'W24' } }], limit: 50 });

    fireEvent.change(screen.getByLabelText('Filter 1: value'), { target: { value: 'S23' } });
    await waitFor(() => expect(screen.getByTestId('url').textContent).toContain('w=attr%7Eyc_batch%7Eeq%7ES23'));
    await waitFor(() => expect(m.calls.filter((c) => c.method === 'POST').at(-1)!.body).toMatchObject({ where: [{ attr: { predicate: 'yc_batch', eq: 'S23' } }] }));

    fireEvent.click(screen.getByRole('button', { name: /Name/ }));
    await waitFor(() => expect(paramsToState(new URLSearchParams(screen.getByTestId('url').textContent!.split('?')[1])).rows).toHaveLength(2));
  });
});
