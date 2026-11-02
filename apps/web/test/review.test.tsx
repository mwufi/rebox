import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createClient, type ReviewItem } from '../src/api';
import { ApiContext, clearCache } from '../src/lib/hooks';
import { ReviewPage, TOKEN_KEY } from '../src/pages/Review';
import { PREDICATES, QUILLWORK, mockFetch, ref } from './fixtures';

const DUPE = { ...QUILLWORK, id: 'rbx:company:quillwork-inc', name: 'Quillwork Inc', facts: QUILLWORK.facts.slice(0, 3), incoming: [] };

const ITEM: ReviewItem = {
  id: 7,
  kind: 'possible_duplicate',
  entity_ids: [QUILLWORK.id, DUPE.id],
  reason: 'same website, different names',
  evidence: [{ key: 'website' }],
  status: 'open',
  resolved_by: null,
  created_at: '2026-10-31T10:00:00.000Z',
  resolved_at: null,
  candidates: [ref(QUILLWORK.id, 'Quillwork'), ref(DUPE.id, 'Quillwork Inc')],
};

function setup(extra: Parameters<typeof mockFetch>[0] = {}) {
  clearCache();
  const m = mockFetch({
    'GET /v0/predicates': () => PREDICATES,
    'GET /v0/review?status=open': (_b, init) =>
      (init.headers as Record<string, string>).authorization === 'Bearer good-token'
        ? [ITEM]
        : new Response(JSON.stringify({ error: 'unauthorized', message: 'invalid or missing credentials' }), { status: 401 }),
    'GET /v0/entities/rbx%3Acompany%3Aquillwork': () => QUILLWORK,
    'GET /v0/entities/rbx%3Acompany%3Aquillwork-inc': () => DUPE,
    'POST /v0/admin/merge': (b) => ({ merged: b, review_id: 7, seq: 100 }),
    'POST /v0/admin/review/7/dismiss': () => ({ ...ITEM, status: 'dismissed' }),
    ...extra,
  });
  render(
    <ApiContext.Provider value={createClient({ fetch: m.fetch })}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ReviewPage />
      </MemoryRouter>
    </ApiContext.Provider>,
  );
  return m;
}

async function unlock(token = 'good-token') {
  fireEvent.change(screen.getByLabelText('Admin token'), { target: { value: token } });
  fireEvent.click(screen.getByRole('button', { name: 'Unlock' }));
}

describe('ReviewPage', () => {
  it('asks for the token, keeps it in sessionStorage only, and lists items', async () => {
    const m = setup();
    expect(m.calls).toHaveLength(0);
    await unlock();
    const card = await screen.findByRole('article', { name: 'Review item 7' });
    expect(within(card).getByText('same website, different names')).toBeTruthy();
    expect(within(card).getByRole('link', { name: 'Quillwork Inc' })).toBeTruthy();
    expect(sessionStorage.getItem(TOKEN_KEY)).toBe('good-token');
    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(m.calls.find((c) => c.url.startsWith('/v0/review'))!.headers.authorization).toBe('Bearer good-token');
    // Side-by-side summaries load each candidate; shared values are marked.
    await waitFor(() => expect(within(card).getAllByTitle('Same on every candidate').length).toBeGreaterThan(0));
  });

  it('merges the other candidates into the kept one, resolving the review item', async () => {
    const m = setup();
    await unlock();
    const card = await screen.findByRole('article', { name: 'Review item 7' });
    const keep = within(card).getAllByRole('button', { name: 'Keep this, merge others in' });
    fireEvent.click(keep[0]!);
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Review item 7' })).toBeNull());
    const merges = m.calls.filter((c) => c.url === '/v0/admin/merge');
    expect(merges).toHaveLength(1);
    expect(merges[0]!.method).toBe('POST');
    expect(merges[0]!.body).toEqual({ from: DUPE.id, into: QUILLWORK.id, review_id: 7 });
    expect(merges[0]!.headers.authorization).toBe('Bearer good-token');
    expect(screen.getByRole('status').textContent).toContain('Merged Quillwork Inc into Quillwork');
  });

  it('dismisses an item', async () => {
    const m = setup();
    await unlock();
    const card = await screen.findByRole('article', { name: 'Review item 7' });
    fireEvent.click(within(card).getByRole('button', { name: /dismiss/i }));
    await waitFor(() => expect(screen.queryByRole('article', { name: 'Review item 7' })).toBeNull());
    const d = m.calls.filter((c) => c.method === 'POST');
    expect(d.map((c) => c.url)).toEqual(['/v0/admin/review/7/dismiss']);
    expect(d[0]!.headers.authorization).toBe('Bearer good-token');
  });

  it('shows API errors and keeps the item', async () => {
    setup({
      'POST /v0/admin/merge': () =>
        new Response(JSON.stringify({ error: 'validation', details: [{ index: -1, message: 'rbx:company:quillwork-inc is already merged' }] }), { status: 400 }),
    });
    await unlock();
    const card = await screen.findByRole('article', { name: 'Review item 7' });
    fireEvent.click(within(card).getAllByRole('button', { name: 'Keep this, merge others in' })[0]!);
    expect((await within(card).findByRole('alert')).textContent).toContain('already merged');
    expect(screen.getByRole('article', { name: 'Review item 7' })).toBeTruthy();
  });

  it('forgets a rejected token', async () => {
    setup();
    await unlock('bad-token');
    expect((await screen.findByRole('alert')).textContent).toContain('not accepted');
    expect(sessionStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(screen.getByLabelText('Admin token')).toBeTruthy();
  });
});
