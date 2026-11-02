/** /api — endpoint list with copyable curl examples against the current host. */
import { CopyButton } from '../components/bits';
import { useTitle } from '../lib/hooks';

interface Endpoint {
  method: 'GET' | 'POST';
  path: string;
  desc: string;
  auth?: 'admin' | 'ingest';
  curl: (origin: string) => string;
}

const q = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;

export const ENDPOINTS: Endpoint[] = [
  { method: 'GET', path: '/health', desc: 'Liveness and database check.', curl: (o) => `curl -s ${o}/health` },
  { method: 'GET', path: '/v0/stats', desc: 'Entity counts by kind, claim counts per layer, and the latest change sequence.', curl: (o) => `curl -s ${o}/v0/stats` },
  { method: 'GET', path: '/v0/predicates', desc: 'The vocabulary: attribute and edge types, their kinds, cardinality and labels.', curl: (o) => `curl -s ${o}/v0/predicates` },
  {
    method: 'GET',
    path: '/v0/search?q=&kind=&limit=20',
    desc: 'Fuzzy name search (trigram). Merged entities are excluded.',
    curl: (o) => `curl -s ${q(`${o}/v0/search?q=quill&kind=company`)}`,
  },
  {
    method: 'GET',
    path: '/v0/entities/:id',
    desc: 'An entity with every effective fact (after the fold) and incoming edges, each with layer, source and source_ref. A merged id returns {"redirect", "entity"}.',
    curl: (o) => `curl -s ${o}/v0/entities/rbx:company:quillwork`,
  },
  { method: 'GET', path: '/v0/entities?ids=a,b', desc: 'EntityRefs for up to 200 ids, merge-followed.', curl: (o) => `curl -s ${q(`${o}/v0/entities?ids=rbx:company:quillwork,rbx:person:odile-fernsby`)}` },
  {
    method: 'GET',
    path: '/v0/entities/:id/edges?predicate=&dir=out|in&limit=50&cursor=',
    desc: 'Pages through all edges of one entity, optionally one predicate and direction.',
    curl: (o) => `curl -s ${q(`${o}/v0/entities/rbx:company:quillwork/edges?dir=in&predicate=worked_at`)}`,
  },
  {
    method: 'POST',
    path: '/v0/query',
    desc: 'AND of filters: name contains, edge (predicate, direction, target, during), attribute (eq, gte, lte, contains). Results say why they matched.',
    curl: (o) =>
      `curl -s ${o}/v0/query \\\n  -H 'content-type: application/json' \\\n  -d '${JSON.stringify({ find: 'person', where: [{ edge: { predicate: 'worked_at', dir: 'out', during: { from: '2018-01-01', to: '2021-12-31' } } }], limit: 20 })}'`,
  },
  { method: 'GET', path: '/v0/changes?after=0&limit=500', desc: 'The change feed in commit order; follow it with ?after=<next>.', curl: (o) => `curl -s ${q(`${o}/v0/changes?after=0&limit=50`)}` },
  {
    method: 'GET',
    path: '/v0/review?status=open',
    auth: 'admin',
    desc: 'Review queue items (possible duplicates) with candidate EntityRefs.',
    curl: (o) => `curl -s ${o}/v0/review -H "authorization: Bearer $ADMIN_TOKEN"`,
  },
  {
    method: 'POST',
    path: '/v0/admin/merge',
    auth: 'admin',
    desc: 'Merge `from` into `into` (both un-merged, same kind). Optionally resolves a review item. Logged and emitted on the change feed.',
    curl: (o) =>
      `curl -s ${o}/v0/admin/merge -H "authorization: Bearer $ADMIN_TOKEN" \\\n  -H 'content-type: application/json' \\\n  -d '{"from":"rbx:company:quillwork-inc","into":"rbx:company:quillwork","review_id":1}'`,
  },
  {
    method: 'POST',
    path: '/v0/admin/split',
    auth: 'admin',
    desc: 'Undo a merge for one entity.',
    curl: (o) =>
      `curl -s ${o}/v0/admin/split -H "authorization: Bearer $ADMIN_TOKEN" \\\n  -H 'content-type: application/json' -d '{"id":"rbx:company:quillwork-inc"}'`,
  },
  {
    method: 'POST',
    path: '/v0/admin/review/:id/dismiss',
    auth: 'admin',
    desc: 'Mark a review item as not a duplicate.',
    curl: (o) => `curl -s -X POST ${o}/v0/admin/review/1/dismiss -H "authorization: Bearer $ADMIN_TOKEN"`,
  },
];

export function ApiPage() {
  useTitle('API');
  const origin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost:3000';
  return (
    <div className="wrap">
      <header style={{ paddingTop: 34 }}>
        <span className="eyebrow">Read API v0</span>
        <h1 className="page-title" style={{ marginTop: 8 }}>
          The graph, as JSON
        </h1>
        <p className="lede" style={{ marginTop: 10 }}>
          Everything this explorer shows comes from these endpoints. Reads are public and anonymous, with CORS open for GET. Entities appear as{' '}
          <code className="mono">{'{id, kind, name}'}</code>; every fact carries <code className="mono">layer</code>, <code className="mono">source</code> and{' '}
          <code className="mono">source_ref</code>.
        </p>
      </header>
      <ul className="endpoints">
        {ENDPOINTS.map((e) => {
          const cmd = e.curl(origin);
          return (
            <li className="endpoint" key={`${e.method} ${e.path}`}>
              <div>
                <div className="sig">
                  <span className={`verb ${e.method.toLowerCase()}`}>{e.method}</span>
                  {e.path}
                </div>
                <p className="desc">{e.desc}</p>
                {e.auth && <span className="chip auth">Bearer {e.auth} token</span>}
              </div>
              <pre className="curl">
                <span className="p">$ </span>
                {cmd}
                <CopyButton text={cmd} label={`Copy curl for ${e.method} ${e.path}`} />
              </pre>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
