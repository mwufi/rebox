import { vi } from 'vitest';
import type { EntityView, Fact, Predicate } from '../src/api';

export const ref = (id: string, name: string | null = null) => ({ id, kind: id.split(':')[1]!, name });

export function fact(p: Partial<Fact> & Pick<Fact, 'predicate'>): Fact {
  return {
    value: null,
    object: null,
    subject: ref('rbx:person:odile', 'Odile Fernsby'),
    valid_from: null,
    valid_to: null,
    layer: 'public/manual',
    source: 'manual',
    source_ref: `demo:${p.predicate}`,
    updated_at: '2026-10-30T12:00:00.000Z',
    ...p,
  };
}

export const PREDICATES: Predicate[] = [
  { id: 'name', subject_kinds: ['person', 'company', 'launch', 'event', 'place'], object_kind: null, cardinality: 'one', meta: { label: 'Name', value_type: 'text', filterable: true } },
  { id: 'description', subject_kinds: ['person', 'company'], object_kind: null, cardinality: 'one', meta: { label: 'Description', value_type: 'text' } },
  { id: 'headline', subject_kinds: ['person'], object_kind: null, cardinality: 'one', meta: { label: 'Headline', value_type: 'text' } },
  { id: 'domain', subject_kinds: ['company'], object_kind: null, cardinality: 'many', meta: { label: 'Domain', value_type: 'text', identity: true } },
  { id: 'website', subject_kinds: ['person', 'company'], object_kind: null, cardinality: 'many', meta: { label: 'Website', value_type: 'url', identity: true } },
  { id: 'yc_batch', subject_kinds: ['company'], object_kind: null, cardinality: 'one', meta: { label: 'YC batch', value_type: 'text', filterable: true } },
  { id: 'founded_year', subject_kinds: ['company'], object_kind: null, cardinality: 'one', meta: { label: 'Founded', value_type: 'int', filterable: true } },
  { id: 'x_followers', subject_kinds: ['person', 'company'], object_kind: null, cardinality: 'one', meta: { label: 'X followers', value_type: 'int', filterable: true } },
  { id: 'hn_user', subject_kinds: ['person'], object_kind: null, cardinality: 'one', meta: { label: 'Hacker News', value_type: 'text', identity: true } },
  { id: 'email', subject_kinds: ['person'], object_kind: null, cardinality: 'many', meta: { label: 'Email', private: true } },
  { id: 'worked_at', subject_kinds: ['person'], object_kind: 'company', cardinality: 'many', meta: { label: 'Worked at', filterable: true } },
  { id: 'founded', subject_kinds: ['person'], object_kind: 'company', cardinality: 'many', meta: { label: 'Founded', filterable: true } },
  { id: 'based_in', subject_kinds: ['person', 'company'], object_kind: 'place', cardinality: 'many', meta: { label: 'Based in', filterable: true } },
  { id: 'knows', subject_kinds: ['person'], object_kind: 'person', cardinality: 'many', meta: { label: 'Knows', private: true } },
];
export const PRED_INDEX = new Map(PREDICATES.map((p) => [p.id, p]));

const quill = ref('rbx:company:quillwork', 'Quillwork');
const halyard = ref('rbx:company:halyard', 'Halyard Data');
const tide = ref('rbx:company:tidewater', 'Tidewater Labs');
const sf = ref('rbx:place:san-francisco', 'San Francisco');
const me = ref('rbx:person:odile', 'Odile Fernsby');

export const ODILE: EntityView = {
  id: me.id,
  kind: 'person',
  name: 'Odile Fernsby',
  merged_from: ['rbx:person:odile-hn'],
  facts: [
    fact({ predicate: 'name', value: 'Odile Fernsby' }),
    fact({ predicate: 'headline', value: 'CEO at Quillwork', layer: 'public/x', source: 'x', source_ref: 'x:odile:bio' }),
    fact({ predicate: 'hn_user', value: 'odilef', layer: 'public/hn', source: 'hn', source_ref: 'hn:user:odilef' }),
    fact({ predicate: 'worked_at', object: halyard, value: { title: 'Product Manager' }, valid_from: '2017-03-01T00:00:00.000Z', valid_to: '2021-08-31T00:00:00.000Z' }),
    fact({ predicate: 'worked_at', object: quill, value: { title: 'CEO' }, valid_from: '2023-06-01T00:00:00.000Z', layer: 'public/wikidata', source: 'wikidata', source_ref: 'wikidata:Q1:P108' }),
    fact({ predicate: 'worked_at', object: tide, value: { title: 'Advisor' }, valid_from: '2022-01-01T00:00:00.000Z', valid_to: '2022-12-31T00:00:00.000Z' }),
    fact({ predicate: 'founded', object: quill }),
    fact({ predicate: 'based_in', object: sf }),
  ],
  incoming: [
    fact({ predicate: 'knows', subject: ref('rbx:person:tomas', 'Tomás Reyes-Oduya'), object: me }),
  ],
};

export const QUILLWORK: EntityView = {
  id: quill.id,
  kind: 'company',
  name: 'Quillwork',
  merged_from: [],
  facts: [
    fact({ predicate: 'name', value: 'Quillwork', subject: quill }),
    fact({ predicate: 'description', value: 'Drafting copilot for small law firms.', subject: quill, layer: 'public/wikidata', source: 'wikidata', source_ref: 'wikidata:Q2:desc' }),
    fact({ predicate: 'founded_year', value: 2023, subject: quill, layer: 'public/wikidata', source: 'wikidata', source_ref: 'wikidata:Q2:P571' }),
    fact({ predicate: 'yc_batch', value: 'W24', subject: quill }),
    fact({ predicate: 'domain', value: 'quillwork.example', subject: quill }),
    fact({ predicate: 'website', value: 'https://quillwork.example', subject: quill }),
    fact({ predicate: 'based_in', object: sf, subject: quill }),
  ],
  incoming: [
    fact({ predicate: 'founded', subject: me, object: quill }),
    fact({ predicate: 'founded', subject: ref('rbx:person:tomas', 'Tomás Reyes-Oduya'), object: quill }),
    fact({ predicate: 'worked_at', subject: me, object: quill, value: { title: 'CEO' }, valid_from: '2023-06-01T00:00:00.000Z' }),
  ],
};

/** A fetch mock that routes by "METHOD path" and records calls. */
export function mockFetch(routes: Record<string, (body: unknown, init: RequestInit) => unknown | Response>) {
  const calls: { method: string; url: string; body: unknown; headers: Record<string, string> }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? 'GET').toUpperCase();
    const body = init.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body, headers: (init.headers ?? {}) as Record<string, string> });
    const path = url.replace(/^https?:\/\/[^/]+/, '');
    const key = Object.keys(routes).find((k) => {
      const [m, p] = k.split(' ') as [string, string];
      return m === method && (p.endsWith('*') ? path.startsWith(p.slice(0, -1)) : path === p);
    });
    if (!key) return new Response(JSON.stringify({ error: 'not_found', message: `no mock for ${method} ${path}` }), { status: 404 });
    const out = routes[key]!(body, init);
    if (out instanceof Response) return out;
    return new Response(JSON.stringify(out), { status: 200, headers: { 'content-type': 'application/json' } });
  });
  return { fetch: fn as unknown as typeof fetch, calls };
}
