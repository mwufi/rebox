/**
 * Seeds a small, entirely FICTIONAL slice of the San Francisco startup world through the claims
 * writer (POST /internal/claims), for demos and the explorer's live check. Every person, company,
 * launch and event here is invented; domains use the reserved `.example` TLD and all ids start
 * with `demo-`. Do not run it against a production database.
 *
 *   API_URL=http://localhost:3000 INGEST_TOKEN=... node scripts/seed-demo.ts
 *
 * Idempotent: re-running rewrites the same claims (same layer + source_ref + subject + predicate).
 */

export {};

type Ref = { id: string } | { mint: { id: string; kind: string } };
interface Claim {
  subject: Ref;
  predicate: string;
  object?: Ref;
  value?: unknown;
  valid_from?: string | null;
  valid_to?: string | null;
  source_ref: string;
}

const API = (process.env.API_URL ?? 'http://localhost:3000').replace(/\/$/, '');
const TOKEN = process.env.INGEST_TOKEN;
if (!TOKEN) {
  console.error('INGEST_TOKEN is required');
  process.exit(2);
}

const P = (slug: string) => `rbx:person:demo-${slug}`;
const C = (slug: string) => `rbx:company:demo-${slug}`;
const L = (slug: string) => `rbx:launch:demo-${slug}`;
const E = (slug: string) => `rbx:event:demo-${slug}`;
const SF = 'rbx:place:san-francisco';
const OAK = 'rbx:place:demo-oakland';
const PA = 'rbx:place:demo-palo-alto';

const layers = new Map<string, Claim[]>();
function add(layer: string, c: Claim) {
  const list = layers.get(layer);
  if (list) list.push(c);
  else layers.set(layer, [c]);
}
const kindOf = (id: string) => id.split(':')[1]!;
const ref = (id: string): Ref => ({ id });
const attr = (layer: string, id: string, predicate: string, value: unknown) =>
  add(layer, { subject: ref(id), predicate, value, source_ref: `demo:${layer.split('/')[1]}:${id}:${predicate}:${String(value)}` });
const edge = (layer: string, s: string, predicate: string, o: string, extra: Partial<Claim> = {}) =>
  add(layer, { subject: ref(s), predicate, object: ref(o), source_ref: `demo:${layer.split('/')[1]}:${s}:${predicate}:${o}`, ...extra });

// ── Places ──────────────────────────────────────────────────────────────────────
attr('public/manual', SF, 'name', 'San Francisco');
attr('public/manual', OAK, 'name', 'Oakland');
attr('public/manual', PA, 'name', 'Palo Alto');

// ── Companies ───────────────────────────────────────────────────────────────────
interface Co {
  slug: string;
  name: string;
  desc: string;
  domain: string;
  year: number;
  batch?: string;
  headcount?: number;
  place: string;
  x?: string;
  gh?: string;
}
const companies: Co[] = [
  { slug: 'halyard-data', name: 'Halyard Data', desc: 'Streaming warehouse for operational analytics; the alumni network behind half the infra startups on this list.', domain: 'halyard.example', year: 2014, headcount: 640, place: SF, x: 'halyarddata', gh: 'halyard' },
  { slug: 'quillwork', name: 'Quillwork', desc: 'Drafting copilot for small law firms: turns intake calls into first drafts.', domain: 'quillwork.example', year: 2023, batch: 'W24', headcount: 11, place: SF, x: 'quillworkhq', gh: 'quillwork' },
  { slug: 'larkspur-robotics', name: 'Larkspur Robotics', desc: 'Pick-and-place arms for mid-size grocery warehouses.', domain: 'larkspur.example', year: 2021, batch: 'S22', headcount: 38, place: OAK, x: 'larkspurbots' },
  { slug: 'tidewater-labs', name: 'Tidewater Labs', desc: 'Preview environments for every pull request, built on spare cloud capacity.', domain: 'tidewater.example', year: 2020, batch: 'W21', headcount: 54, place: SF, gh: 'tidewater-labs' },
  { slug: 'mossgate', name: 'Mossgate', desc: 'Payroll and benefits for restaurant groups.', domain: 'mossgate.example', year: 2017, headcount: 210, place: SF },
  { slug: 'brightkiln', name: 'Brightkiln', desc: 'Thermal batteries that store cheap midday solar for industrial heat.', domain: 'brightkiln.example', year: 2022, batch: 'S23', headcount: 19, place: OAK },
  { slug: 'copperleaf-ai', name: 'Copperleaf AI', desc: 'Evaluation harnesses for teams shipping LLM features.', domain: 'copperleaf.example', year: 2022, batch: 'W23', headcount: 23, place: SF, gh: 'copperleaf-ai', x: 'copperleafai' },
  { slug: 'fernhill-bio', name: 'Fernhill Bio', desc: 'Cloud lab automation for early-stage biotech.', domain: 'fernhill.example', year: 2019, headcount: 72, place: PA },
  { slug: 'northbeam-freight', name: 'Northbeam Freight', desc: 'Freight brokerage software for regional carriers.', domain: 'northbeam.example', year: 2018, headcount: 120, place: SF },
  { slug: 'pebble-ventures', name: 'Pebble Ventures', desc: 'Pre-seed fund backing technical founders in the Bay Area.', domain: 'pebble.example', year: 2016, headcount: 6, place: SF },
];
for (const c of companies) {
  const id = C(c.slug);
  attr('public/manual', id, 'name', c.name);
  attr('public/wikidata', id, 'description', c.desc);
  attr('public/manual', id, 'domain', c.domain);
  attr('public/manual', id, 'website', `https://${c.domain}`);
  attr('public/wikidata', id, 'founded_year', c.year);
  if (c.batch) attr('public/manual', id, 'yc_batch', c.batch);
  if (c.headcount) attr('public/manual', id, 'headcount', c.headcount);
  if (c.x) attr('public/x', id, 'x_handle', c.x);
  if (c.gh) attr('public/github', id, 'github', c.gh);
  edge('public/manual', id, 'based_in', c.place);
}
// A probable duplicate of Quillwork, as a different source might produce (for the review queue).
attr('public/hn', C('quillwork-inc'), 'name', 'Quillwork Inc');
attr('public/hn', C('quillwork-inc'), 'website', 'https://quillwork.example');
attr('public/hn', C('quillwork-inc'), 'founded_year', 2023);
edge('public/manual', C('pebble-ventures'), 'invested_in', C('quillwork'), { valid_from: '2024-04-02' });
edge('public/manual', C('pebble-ventures'), 'invested_in', C('brightkiln'), { valid_from: '2023-09-12' });
edge('public/manual', C('pebble-ventures'), 'invested_in', C('copperleaf-ai'), { valid_from: '2023-04-20' });

// ── People ──────────────────────────────────────────────────────────────────────
interface Job {
  at: string;
  title: string;
  from: string;
  to?: string;
}
interface Person {
  slug: string;
  name: string;
  headline: string;
  place: string;
  jobs: Job[];
  founded?: string[];
  ceo?: string;
  hn?: string;
  x?: [string, number];
  gh?: string;
}
const people: Person[] = [
  { slug: 'odile-fernsby', name: 'Odile Fernsby', headline: 'Co-founder & CEO at Quillwork. Previously legal-tech PM.', place: SF, jobs: [{ at: 'halyard-data', title: 'Product Manager', from: '2017-03-01', to: '2021-08-31' }, { at: 'quillwork', title: 'CEO', from: '2023-06-01' }], founded: ['quillwork'], ceo: 'quillwork', hn: 'odilef', x: ['odilefernsby', 4210] },
  { slug: 'tomas-reyes-oduya', name: 'Tomás Reyes-Oduya', headline: 'Co-founder & CTO at Quillwork.', place: SF, jobs: [{ at: 'halyard-data', title: 'Staff Engineer', from: '2016-01-11', to: '2022-02-28' }, { at: 'tidewater-labs', title: 'Founding Engineer', from: '2022-03-01', to: '2023-05-31' }, { at: 'quillwork', title: 'CTO', from: '2023-06-01' }], founded: ['quillwork'], gh: 'treyes', hn: 'tro' },
  { slug: 'maren-lockhart', name: 'Maren Lockhart', headline: 'Robotics. Founder of Larkspur.', place: OAK, jobs: [{ at: 'larkspur-robotics', title: 'CEO', from: '2021-02-01' }], founded: ['larkspur-robotics'], ceo: 'larkspur-robotics', x: ['marenlockhart', 9800] },
  { slug: 'idris-calloway', name: 'Idris Calloway', headline: 'Controls engineer; co-founder at Larkspur Robotics.', place: OAK, jobs: [{ at: 'halyard-data', title: 'Software Engineer', from: '2015-06-01', to: '2018-12-31' }, { at: 'larkspur-robotics', title: 'CTO', from: '2021-02-01' }], founded: ['larkspur-robotics'], gh: 'idrisc' },
  { slug: 'priya-venkataraman-holt', name: 'Priya Venkataraman-Holt', headline: 'Founder at Tidewater Labs. Ex-Halyard infra lead.', place: SF, jobs: [{ at: 'halyard-data', title: 'Infrastructure Lead', from: '2014-09-01', to: '2020-03-31' }, { at: 'tidewater-labs', title: 'CEO', from: '2020-04-01' }], founded: ['tidewater-labs'], ceo: 'tidewater-labs', hn: 'pvh', gh: 'pvholt', x: ['pvholt', 15400] },
  { slug: 'august-nakamura-bell', name: 'August Nakamura-Bell', headline: 'Builds payroll systems that restaurants do not hate.', place: SF, jobs: [{ at: 'mossgate', title: 'CEO', from: '2017-05-01' }], founded: ['mossgate'], ceo: 'mossgate' },
  { slug: 'selin-achterberg', name: 'Selin Achterberg', headline: 'Thermal storage. Co-founder of Brightkiln.', place: OAK, jobs: [{ at: 'fernhill-bio', title: 'Hardware Engineer', from: '2019-07-01', to: '2022-05-31' }, { at: 'brightkiln', title: 'CEO', from: '2022-06-01' }], founded: ['brightkiln'], ceo: 'brightkiln', x: ['selinbuilds', 2300] },
  { slug: 'kofi-almeida', name: 'Kofi Almeida', headline: 'Evals, agents, and boring reliability work.', place: SF, jobs: [{ at: 'halyard-data', title: 'ML Engineer', from: '2018-02-01', to: '2022-01-31' }, { at: 'copperleaf-ai', title: 'CEO', from: '2022-02-01' }], founded: ['copperleaf-ai'], ceo: 'copperleaf-ai', hn: 'kofia', gh: 'kofialmeida', x: ['kofialmeida', 7600] },
  { slug: 'wren-castellanos', name: 'Wren Castellanos', headline: 'Co-founder at Copperleaf AI.', place: SF, jobs: [{ at: 'tidewater-labs', title: 'Engineer', from: '2020-09-01', to: '2022-01-31' }, { at: 'copperleaf-ai', title: 'CTO', from: '2022-02-01' }], founded: ['copperleaf-ai'], gh: 'wrenc' },
  { slug: 'hollis-ferrante', name: 'Hollis Ferrante', headline: 'Lab automation. Founder of Fernhill Bio.', place: PA, jobs: [{ at: 'fernhill-bio', title: 'CEO', from: '2019-01-01' }], founded: ['fernhill-bio'], ceo: 'fernhill-bio' },
  { slug: 'dagny-oyelaran', name: 'Dagny Oyelaran', headline: 'Freight + software. Northbeam founder.', place: SF, jobs: [{ at: 'halyard-data', title: 'Solutions Engineer', from: '2014-11-01', to: '2017-12-31' }, { at: 'northbeam-freight', title: 'CEO', from: '2018-01-01' }], founded: ['northbeam-freight'], ceo: 'northbeam-freight' },
  { slug: 'ruth-abernathy-lowe', name: 'Ruth Abernathy-Lowe', headline: 'General partner at Pebble Ventures.', place: SF, jobs: [{ at: 'halyard-data', title: 'VP Engineering', from: '2014-03-01', to: '2016-06-30' }, { at: 'pebble-ventures', title: 'General Partner', from: '2016-07-01' }], founded: ['pebble-ventures'], x: ['ruthal', 21000] },
];
// Halyard alumni without their own company: makes Halyard a hub (tests "+N more" in the graph).
const alumni: [string, string, string, string, string?][] = [
  ['bram-oconnell', 'Bram O’Connell', 'Data Engineer', '2016-04-01', '2019-10-31'],
  ['ines-takahashi', 'Inés Takahashi', 'Engineering Manager', '2015-02-01'],
  ['lior-mensah', 'Lior Mensah', 'Site Reliability Engineer', '2017-08-01', '2021-03-31'],
  ['agnes-whitford', 'Agnes Whitford', 'Designer', '2018-01-15'],
  ['caspian-duarte', 'Caspian Duarte', 'Software Engineer', '2019-06-01', '2023-02-28'],
  ['noor-halvorsen', 'Noor Halvorsen', 'Product Manager', '2020-01-06'],
  ['emeka-sorensen', 'Emeka Sorensen', 'Staff Engineer', '2016-09-01', '2024-01-31'],
  ['juno-park-adler', 'Juno Park-Adler', 'Data Scientist', '2021-05-01'],
  ['silas-oyelowo', 'Silas Oyelowo', 'Security Engineer', '2019-02-01'],
  ['tamsin-greaves', 'Tamsin Greaves', 'Developer Advocate', '2022-03-01'],
];
for (const [slug, name, title, from, to] of alumni) {
  people.push({ slug, name, headline: `${title} at Halyard Data`, place: SF, jobs: [{ at: 'halyard-data', title, from, ...(to ? { to } : {}) }] });
}
// Later moves for a few alumni, so the 2-hop graph has texture.
people.find((p) => p.slug === 'bram-oconnell')!.jobs.push({ at: 'tidewater-labs', title: 'Data Engineer', from: '2019-11-01' });
people.find((p) => p.slug === 'lior-mensah')!.jobs.push({ at: 'copperleaf-ai', title: 'Founding Engineer', from: '2022-04-01' });
people.find((p) => p.slug === 'caspian-duarte')!.jobs.push({ at: 'quillwork', title: 'Founding Engineer', from: '2023-09-01' });

for (const p of people) {
  const id = P(p.slug);
  attr('public/manual', id, 'name', p.name);
  attr(p.x ? 'public/x' : 'public/manual', id, 'headline', p.headline);
  edge('public/manual', id, 'based_in', p.place);
  for (const j of p.jobs) {
    edge(j.at === 'halyard-data' && !p.gh ? 'public/wikidata' : 'public/manual', id, 'worked_at', C(j.at), {
      value: { title: j.title },
      valid_from: j.from,
      valid_to: j.to ?? null,
    });
  }
  for (const f of p.founded ?? []) edge('public/wikidata', id, 'founded', C(f));
  if (p.ceo) edge('public/manual', id, 'ceo_of', C(p.ceo));
  if (p.hn) attr('public/hn', id, 'hn_user', p.hn);
  if (p.gh) attr('public/github', id, 'github', p.gh);
  if (p.x) {
    attr('public/x', id, 'x_handle', p.x[0]);
    attr('public/x', id, 'x_followers', p.x[1]);
  }
}
// Angels.
edge('public/manual', P('ruth-abernathy-lowe'), 'invested_in', C('tidewater-labs'), { valid_from: '2020-06-15' });
edge('public/manual', P('priya-venkataraman-holt'), 'invested_in', C('copperleaf-ai'), { valid_from: '2022-08-01' });
edge('public/manual', P('dagny-oyelaran'), 'invested_in', C('larkspur-robotics'), { valid_from: '2021-10-10' });

// ── Launches (HN) ───────────────────────────────────────────────────────────────
const launches: [string, string, string, string, number, string, string][] = [
  ['quillwork-launch-hn', 'Launch HN: Quillwork (YC W24) – first drafts for small law firms', 'quillwork', 'odile-fernsby', 312, '2024-02-20', '90000001'],
  ['tidewater-show-hn', 'Show HN: Tidewater – a preview environment for every PR', 'tidewater-labs', 'priya-venkataraman-holt', 541, '2021-01-12', '90000002'],
  ['copperleaf-show-hn', 'Show HN: Copperleaf – regression tests for LLM prompts', 'copperleaf-ai', 'kofi-almeida', 428, '2023-02-07', '90000003'],
  ['larkspur-launch-hn', 'Launch HN: Larkspur Robotics (YC S22) – arms for grocery warehouses', 'larkspur-robotics', 'maren-lockhart', 187, '2022-07-26', '90000004'],
  ['brightkiln-launch-hn', 'Launch HN: Brightkiln (YC S23) – thermal batteries for factory heat', 'brightkiln', 'selin-achterberg', 266, '2023-08-01', '90000005'],
  ['tidewater-v2', 'Tidewater 2.0: ephemeral databases for preview environments', 'tidewater-labs', 'bram-oconnell', 143, '2024-05-14', '90000006'],
];
for (const [slug, title, co, poster, points, day, story] of launches) {
  const id = L(slug);
  attr('public/hn', id, 'name', title);
  attr('public/hn', id, 'hn_story', story);
  attr('public/hn', id, 'points', points);
  attr('public/hn', id, 'url', `https://${co.split('-')[0]}.example/launch`);
  edge('public/hn', C(co), 'launched', id, { valid_from: day });
  edge('public/hn', P(poster), 'posted', id, { valid_from: day });
}

// ── Events ──────────────────────────────────────────────────────────────────────
attr('public/manual', E('infra-supper-club-2025'), 'name', 'Infra Supper Club, Spring 2025');
attr('public/manual', E('infra-supper-club-2025'), 'description', 'A fictional monthly dinner for infrastructure founders in SoMa.');
attr('public/manual', E('infra-supper-club-2025'), 'url', 'https://supperclub.example/2025-spring');
for (const [p, role] of [
  ['priya-venkataraman-holt', 'host'],
  ['kofi-almeida', 'speaker'],
  ['tomas-reyes-oduya', 'guest'],
  ['wren-castellanos', 'guest'],
  ['ruth-abernathy-lowe', 'guest'],
] as const) {
  edge('public/manual', P(p), 'attended', E('infra-supper-club-2025'), { value: { role }, valid_from: '2025-04-17', valid_to: '2025-04-17' });
}

// A probable duplicate person from another source (for the review queue).
attr('public/hn', P('odile-fernsby-hn'), 'name', 'Odile Fernsby');
attr('public/hn', P('odile-fernsby-hn'), 'hn_user', 'odile_f');
attr('public/hn', P('odile-fernsby-hn'), 'headline', 'Building Quillwork. Lawyers deserve better tools.');

// ── Write ───────────────────────────────────────────────────────────────────────
// Every subject/object is sent as {mint}: the writer creates it if missing (home layer = the
// request layer) and reuses it otherwise, so layer order only decides each entity's home layer.
// Review items are not claims: the resolver creates them (see the live check in the S5 report).
async function post(layer: string, claims: Claim[]) {
  const res = await fetch(`${API}/internal/claims`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${TOKEN}` },
    body: JSON.stringify({ layer, claims }),
  });
  const body = await res.text();
  if (!res.ok) throw new Error(`${layer}: HTTP ${res.status} ${body}`);
  return JSON.parse(body) as { written: number; minted: number };
}

const order = ['public/manual', 'public/wikidata', 'public/github', 'public/x', 'public/hn'];
let written = 0;
let mintedTotal = 0;
for (const layer of order) {
  const claims = (layers.get(layer) ?? []).map((c) => ({
    ...c,
    subject: { mint: { id: idOf(c.subject), kind: kindOf(idOf(c.subject)) } },
    ...(c.object ? { object: { mint: { id: idOf(c.object), kind: kindOf(idOf(c.object)) } } } : {}),
  }));
  for (let i = 0; i < claims.length; i += 500) {
    const r = await post(layer, claims.slice(i, i + 500));
    written += r.written;
    mintedTotal += r.minted;
    console.log(`${layer.padEnd(16)} ${String(claims.slice(i, i + 500).length).padStart(4)} claims  written=${r.written} minted=${r.minted}`);
  }
}
console.log(`done: ${written} claims written, ${mintedTotal} entities minted (all fictional)`);

function idOf(r: Ref): string {
  return 'id' in r ? r.id : r.mint.id;
}
