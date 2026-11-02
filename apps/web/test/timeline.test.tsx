import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createClient, type Fact, type Timeline, type Wiki } from '../src/api';
import { factWhen, handleOf } from '../src/lib/format';
import { ApiContext, clearCache } from '../src/lib/hooks';
import { Markdown, parseMarkdown } from '../src/lib/markdown';
import { eventDate, groupByYear } from '../src/lib/timeline';
import { EntityPage } from '../src/pages/Entity';
import { TOKEN_KEY } from '../src/pages/Review';
import { ODILE, PREDICATES, fact, mockFetch, ref } from './fixtures';

const quill = ref('rbx:company:quillwork', 'Quillwork');
const launch = ref('rbx:launch:quill-v1', 'Quillwork v1');

const TIMELINE: Timeline = {
  id: ODILE.id,
  next: null,
  events: [
    { key: 'fs', at: '2026-11-01T10:00:00.000Z', kind: 'first_seen', title: 'First seen in Rebox', predicate: 'name', other: null, fact: null, precision: 'time', headline: false, layer: 'public/hn' },
    {
      key: 'j', at: '2023-06-01T00:00:00.000Z', kind: 'joined', title: 'Joined Quillwork as CEO', predicate: 'worked_at', other: quill,
      fact: fact({ predicate: 'worked_at', object: quill, value: { title: 'CEO' }, valid_from: '2023-06-01T00:00:00.000Z', evidence: [{ url: 'https://example.com/post', author: 'odile' }], support: 2 }),
      precision: 'day', headline: false,
    },
    {
      key: 'p', at: '2023-03-14T17:04:00.000Z', kind: 'posted', title: 'Posted Quillwork v1', predicate: 'posted', other: launch,
      fact: fact({ predicate: 'posted', object: launch, valid_from: '2023-03-14T17:04:00.000Z', layer: 'public/hn', source: 'hn', source_ref: 'hn:story:1:posted:launch' }),
      precision: 'time', headline: true,
    },
    {
      key: 'f', at: '2023-01-01T00:00:00.000Z', kind: 'founded', title: 'Founded Quillwork', predicate: 'founded', other: quill,
      fact: fact({ predicate: 'founded', object: quill }), precision: 'year', headline: true,
    },
    {
      key: 'l', at: '2021-08-31T00:00:00.000Z', kind: 'left', title: 'Left Halyard Data', predicate: 'worked_at', other: ref('rbx:company:halyard', 'Halyard Data'),
      fact: fact({ predicate: 'worked_at' }), precision: 'day', headline: false,
    },
  ],
};

const WIKI: Wiki = {
  content_md:
    'Odile Fernsby is the CEO of Quillwork.[^1] Rebox doesn\'t know yet where she studied.\n\n## Career\n- Founded Quillwork in 2023.[^2]\n\n[^1]: [Wikidata Q1](https://www.wikidata.org/wiki/Q1)\n[^2]: [Hacker News item 1](https://news.ycombinator.com/item?id=1)\n',
  model: 'claude-haiku-5-5',
  generated_at: '2026-11-01T12:00:00.000Z',
  input_seq: 42,
  stale: true,
  can_generate: true,
  public: false,
};

function renderEntity(routes: Parameters<typeof mockFetch>[0]) {
  clearCache();
  const m = mockFetch({ 'GET /v0/predicates': () => PREDICATES, 'GET /v0/entities/rbx%3Aperson%3Aodile': () => ODILE, ...routes });
  render(
    <ApiContext.Provider value={createClient({ fetch: m.fetch })}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={['/e/rbx%3Aperson%3Aodile']}>
        <Routes>
          <Route path="/e/:id" element={<EntityPage />} />
        </Routes>
      </MemoryRouter>
    </ApiContext.Provider>,
  );
  return m;
}

const notFoundWiki = () =>
  new Response(JSON.stringify({ error: 'not_found', message: 'no wiki for this entity yet', can_generate: true, public: false }), { status: 404 });

describe('timeline helpers', () => {
  it('groups by year in order and formats dates by precision', () => {
    expect(groupByYear(TIMELINE.events).map((g) => [g.year, g.events.length])).toEqual([['2026', 1], ['2023', 3], ['2021', 1]]);
    expect(eventDate(TIMELINE.events[3]!)).toEqual({ short: '', full: '2023' });
    expect(eventDate(TIMELINE.events[1]!).short).toBe('Jun 1');
    expect(eventDate(TIMELINE.events[2]!).full).toBe('Mar 14, 2023 · 17:04 UTC');
  });

  it('point-in-time predicates show one date, never a range to now', () => {
    const f = (p: Partial<Fact>) => ({ predicate: 'launched', valid_from: null, valid_to: null, ...p });
    expect(factWhen(f({ valid_from: '2018-06-28T17:23:50.000Z' }))).toEqual({ text: 'Jun 2018', current: false });
    expect(factWhen(f({ predicate: 'acquired', valid_from: '2023-01-15T00:00:00.000Z' }))).toEqual({ text: 'Jan 2023', current: false });
    expect(factWhen(f({ predicate: 'worked_at', valid_from: '2019-03-01T00:00:00.000Z' }))).toEqual({ text: 'Mar 2019 –', current: true });
    expect(factWhen(f({ predicate: 'worked_at', valid_from: '2019-03-01T00:00:00.000Z', valid_to: '2021-02-01T00:00:00.000Z' })).text).toBe('Mar 2019 – Feb 2021');
  });

  it('detects handle-only names from the ref hint or the identity facts', () => {
    expect(handleOf({ name: 'hdubugras', handle: 'hn' })).toBe('hn');
    expect(handleOf({ name: 'pg', facts: [{ predicate: 'hn_user', value: 'pg' }] })).toBe('hn');
    expect(handleOf({ name: 'Paul Graham', facts: [{ predicate: 'hn_user', value: 'pg' }] })).toBeNull();
  });
});

describe('markdown', () => {
  it('parses headings, lists and footnotes; renders only http(s) links', () => {
    const doc = parseMarkdown(WIKI.content_md);
    expect(doc.blocks.map((b) => b.type)).toEqual(['p', 'h', 'ul']);
    expect(doc.footnotes.map((f) => f.n)).toEqual(['1', '2']);
    const { container } = render(<Markdown source={'Bad [x](javascript:alert(1)) and <b>raw</b>.[^1]\n\n[^1]: [ok](https://ok.example)'} />);
    const links = [...container.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    expect(links).not.toContain('javascript:alert(1)');
    expect(links).toContain('https://ok.example');
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('<b>raw</b>');
  });
});

describe('EntityPage timeline and wiki', () => {
  it('renders a year-grouped timeline with linked entities, evidence and source badges', async () => {
    renderEntity({ 'GET /v0/entities/rbx%3Aperson%3Aodile/timeline': () => TIMELINE, 'GET /v0/entities/rbx%3Aperson%3Aodile/wiki': notFoundWiki });
    const tl = await screen.findByRole('list', { name: 'Timeline, newest first' });
    expect(within(tl).getByLabelText('Year 2023')).toBeTruthy();
    const items = within(tl).getAllByRole('listitem').filter((li) => li.classList.contains('tl-event'));
    expect(items.map((li) => li.dataset.kind)).toEqual(['first_seen', 'joined', 'posted', 'founded', 'left']);
    // Headline events are marked; the other entity is a link.
    expect(items[2]!.classList.contains('headline')).toBe(true);
    expect(items[1]!.classList.contains('headline')).toBe(false);
    expect(within(items[1]!).getByRole('link', { name: 'Quillwork' }).getAttribute('href')).toBe('/e/rbx%3Acompany%3Aquillwork');
    expect(within(items[1]!).getByRole('link', { name: /Source/ }).getAttribute('href')).toBe('https://example.com/post');
    expect(within(items[1]!).getByText('2 sources')).toBeTruthy();
    expect(within(items[2]!).getByLabelText(/source public\/hn/).textContent).toContain('HN');
    // Year-precision events show no month/day.
    expect(items[3]!.querySelector('time')!.textContent).toBe('');
    expect(screen.getByText('4 events')).toBeTruthy();
  });

  it('shows a cached wiki with footnotes, and regenerates with the admin token', async () => {
    sessionStorage.setItem(TOKEN_KEY, 'tok');
    const fresh: Wiki = { ...WIKI, stale: false, content_md: 'New article.[^1]\n\n[^1]: [Wikidata Q1](https://www.wikidata.org/wiki/Q1)\n' };
    const m = renderEntity({
      'GET /v0/entities/rbx%3Aperson%3Aodile/timeline': () => TIMELINE,
      'GET /v0/entities/rbx%3Aperson%3Aodile/wiki': () => WIKI,
      'POST /v0/entities/rbx%3Aperson%3Aodile/wiki': () => fresh,
    });
    const wiki = await screen.findByRole('region', { name: 'Wiki' });
    await waitFor(() => expect(within(wiki).getByText(/Odile Fernsby is the CEO/)).toBeTruthy());
    expect(within(wiki).getByRole('list', { name: 'Sources' }).textContent).toContain('Wikidata Q1');
    expect(within(wiki).getByRole('link', { name: 'Source 1' }).getAttribute('href')).toMatch(/#wiki-.*-fn-1$/);
    expect(within(wiki).getByText(/Newer facts since/)).toBeTruthy();

    await userEvent.click(within(wiki).getByRole('button', { name: 'Update' }));
    await waitFor(() => expect(within(wiki).getByText('New article.')).toBeTruthy());
    const post = m.calls.find((c) => c.method === 'POST')!;
    expect(post.headers.authorization).toBe('Bearer tok');
    expect(post.url).toBe('/v0/entities/rbx%3Aperson%3Aodile/wiki');
    expect(within(wiki).getByRole('button', { name: 'Regenerate' })).toBeTruthy();
  });

  it('without a token, offers no generation and points to the Review page', async () => {
    renderEntity({ 'GET /v0/entities/rbx%3Aperson%3Aodile/timeline': () => ({ events: [] }), 'GET /v0/entities/rbx%3Aperson%3Aodile/wiki': notFoundWiki });
    const wiki = await screen.findByRole('region', { name: 'Wiki' });
    await waitFor(() => expect(within(wiki).getByText(/No article about Odile Fernsby yet/)).toBeTruthy());
    expect((within(wiki).getByRole('button', { name: 'Generate' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(wiki).getByRole('link', { name: 'Review page' }).getAttribute('href')).toBe('/review');
    expect(await screen.findByText('No dated facts yet.')).toBeTruthy();
  });

  it('marks handle-only names with a hint', async () => {
    clearCache();
    const hn = { ...ODILE, name: 'odilef' };
    const m = mockFetch({
      'GET /v0/predicates': () => PREDICATES,
      'GET /v0/entities/rbx%3Aperson%3Aodile': () => hn,
      'GET /v0/entities/rbx%3Aperson%3Aodile/timeline': () => ({ events: [] }),
      'GET /v0/entities/rbx%3Aperson%3Aodile/wiki': notFoundWiki,
    });
    render(
      <ApiContext.Provider value={createClient({ fetch: m.fetch })}>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={['/e/rbx%3Aperson%3Aodile']}>
          <Routes>
            <Route path="/e/:id" element={<EntityPage />} />
          </Routes>
        </MemoryRouter>
      </ApiContext.Provider>,
    );
    const h1 = await screen.findByRole('heading', { level: 1, name: 'odilef' });
    expect(h1.classList.contains('is-handle')).toBe(true);
    expect(screen.getByText('HN handle')).toBeTruthy();
  });
});
