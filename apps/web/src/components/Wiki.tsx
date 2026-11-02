/**
 * Generated wiki article for an entity (CONTRACTS §14). Generation uses the admin token kept in
 * sessionStorage by the Review page, unless the API says wikis are public.
 */
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, type Wiki } from '../api';
import { relativeTime } from '../lib/format';
import { primeCache, useApi, useAsync } from '../lib/hooks';
import { Markdown } from '../lib/markdown';
import { TOKEN_KEY } from '../pages/Review';
import { Skel } from './bits';

function readToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

type Missing = { missing: true; can_generate: boolean; public: boolean };

function generateError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) return 'Generating needs the admin token.';
    if (err.status === 429) return `Rate limit reached; try again in ${err.body?.retry_after ?? 60}s.`;
    if (err.status === 503) return 'Wiki generation is turned off on this server.';
    if (err.status === 502) return 'The model call failed. Try again in a minute.';
    return err.message;
  }
  return (err as Error)?.message ?? 'Generation failed.';
}

export function WikiSection({ id, name }: { id: string; name: string }) {
  const client = useApi();
  const key = `wiki:${id}`;
  const r = useAsync<Wiki | Missing>(key, (signal) =>
    client.wiki(id, { signal }).catch((err: unknown) => {
      if (err instanceof ApiError && err.status === 404 && err.body?.error === 'not_found' && 'can_generate' in (err.body ?? {})) {
        return { missing: true, can_generate: Boolean(err.body?.can_generate), public: Boolean(err.body?.public) } as Missing;
      }
      throw err;
    }),
  );
  const [local, setLocal] = useState<Wiki | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setLocal(null);
    setError(null);
  }, [id]);

  const data = local ?? r.data;
  const wiki = data && !('missing' in data) ? data : null;
  const canGenerate = data ? Boolean(data.can_generate ?? ('missing' in data ? false : true)) : false;
  const isPublic = Boolean(data?.public);
  const token = readToken();
  const needsToken = canGenerate && !isPublic && !token;

  const generate = async () => {
    setBusy(true);
    setError(null);
    try {
      const w = await client.generateWiki(id, { token, force: Boolean(wiki && !wiki.stale) });
      primeCache(key, w);
      setLocal(w);
    } catch (err) {
      setError(generateError(err));
    } finally {
      setBusy(false);
    }
  };

  const action = canGenerate && (
    <button type="button" className={`btn sm${wiki ? ' ghost' : ' primary'}`} onClick={generate} disabled={busy || needsToken} aria-busy={busy}>
      {busy ? 'Writing…' : wiki ? (wiki.stale ? 'Update' : 'Regenerate') : 'Generate'}
    </button>
  );

  return (
    <section className="wiki" aria-labelledby={`wiki-h-${id}`}>
      <div className="section-head">
        <h2 id={`wiki-h-${id}`}>Wiki</h2>
        {wiki && <span className="count">{wiki.model}</span>}
        <span className="aside">{action}</span>
      </div>
      {r.loading && !data && (
        <div className="wiki-skel" aria-busy="true">
          <Skel w="92%" h={14} />
          <Skel w="84%" h={14} style={{ marginTop: 8 }} />
          <Skel w="60%" h={14} style={{ marginTop: 8 }} />
        </div>
      )}
      {r.error && !data && <p className="empty">Could not load the article: {r.error.message}</p>}
      {busy && !wiki && <p className="wiki-note">Writing an article about {name} from the public facts…</p>}
      {data && !wiki && !busy && (
        <p className="wiki-note">
          No article about {name} yet.{' '}
          {canGenerate
            ? needsToken
              ? (
                  <>
                    Set the admin token on the <Link to="/review">Review page</Link> to generate one.
                  </>
                )
              : 'Generate one from the public facts below; it cites its sources and says what Rebox doesn’t know.'
            : ''}
        </p>
      )}
      {wiki && (
        <article className={`wiki-article fade-in${busy ? ' busy' : ''}`}>
          <Markdown source={wiki.content_md} idPrefix={`wiki-${id.replace(/[^a-z0-9]/gi, '-')}`} />
          <p className="wiki-foot">
            Generated {relativeTime(wiki.generated_at)} from public facts as of change #{wiki.input_seq}.
            {wiki.stale && <span className="stale-chip"> Newer facts since — {canGenerate ? 'update to refresh.' : 'out of date.'}</span>}
          </p>
        </article>
      )}
      {error && (
        <p className="error-box" role="alert">
          {error}
          {needsToken || /admin token/.test(error) ? (
            <>
              {' '}
              <Link to="/review">Set it on the Review page</Link>.
            </>
          ) : null}
        </p>
      )}
    </section>
  );
}
