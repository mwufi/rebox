import { useState, type CSSProperties, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { EntityRef, Fact } from '../api';
import { displayName, kindLabel, scopeOf, sourceCode, sourceHue } from '../lib/format';

export const entityPath = (id: string) => `/e/${encodeURIComponent(id)}`;
export const graphPath = (id: string) => `/e/${encodeURIComponent(id)}/graph`;

export function KindGlyph({ kind }: { kind: string }) {
  return <span className="kind-glyph" data-kind={kind} aria-hidden="true" />;
}

export function KindTag({ kind, plural }: { kind: string; plural?: boolean }) {
  return (
    <span className="kind" data-kind={kind}>
      <KindGlyph kind={kind} />
      {plural ? kindLabel(kind) : kind}
    </span>
  );
}

export function EntityLink({ entity, className }: { entity: EntityRef; className?: string }) {
  return (
    <Link to={entityPath(entity.id)} className={`elink ${className ?? ''}`} title={entity.id} data-kind={entity.kind}>
      <KindGlyph kind={entity.kind} />
      <span className={`elink-name${entity.name ? '' : ' unnamed'}`}>{displayName(entity)}</span>
    </Link>
  );
}

/** A citation mark for one fact: source code + hue; layer and source_ref on hover/focus. */
export function SourceBadge({ fact }: { fact: Pick<Fact, 'source' | 'layer' | 'source_ref' | 'updated_at'> }) {
  const style = { '--h': sourceHue(fact.source) } as CSSProperties;
  const scope = scopeOf(fact.layer);
  return (
    <span
      className="src"
      style={style}
      data-scope={scope}
      tabIndex={0}
      aria-label={`source ${fact.layer}, ref ${fact.source_ref}`}
      title={`${fact.layer} · ${fact.source_ref}`}
    >
      {sourceCode(fact.source)}
      <span className="tip" role="tooltip">
        <b>{fact.layer}</b>
        {'\n'}
        {fact.source_ref}
        {'\n'}
        <span style={{ opacity: 0.65 }}>updated {fact.updated_at.slice(0, 10)}</span>
      </span>
    </span>
  );
}

export function Skel({ w = '100%', h = 14, style }: { w?: number | string; h?: number; style?: CSSProperties }) {
  return <span className="skel" style={{ width: w, height: h, ...style }} aria-hidden="true" />;
}

/** N skeleton rows at the same height as real rows, so content lands without shifting. */
export function SkelRows({ n, h = 41, className = 'facts' }: { n: number; h?: number; className?: string }) {
  return (
    <ul className={className} aria-busy="true" aria-label="loading">
      {Array.from({ length: n }, (_, i) => (
        <li key={i} style={{ minHeight: h }}>
          <Skel w={`${45 + ((i * 37) % 40)}%`} h={13} />
          <span />
          <Skel w={54} h={10} />
        </li>
      ))}
    </ul>
  );
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
      } finally {
        ta.remove();
      }
    }
    setDone(true);
    setTimeout(() => setDone(false), 1200);
  };
  return (
    <button type="button" className={`copy-btn${done ? ' done' : ''}`} onClick={copy} aria-label={done ? 'Copied' : label} title={label}>
      {done ? <Icon name="check" /> : <Icon name="copy" />}
    </button>
  );
}

const PATHS: Record<string, ReactNode> = {
  search: (
    <>
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.5 10.5 14 14" />
    </>
  ),
  copy: (
    <>
      <rect x="5" y="5" width="8" height="8" rx="1" />
      <path d="M3 10V3.5A.5.5 0 0 1 3.5 3H10" />
    </>
  ),
  check: <path d="m3.5 8.5 3 3 6-7" />,
  sun: (
    <>
      <circle cx="8" cy="8" r="3" />
      <path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1" />
    </>
  ),
  moon: <path d="M13 9.5A5.5 5.5 0 0 1 6.5 3a5.5 5.5 0 1 0 6.5 6.5Z" />,
  graph: (
    <>
      <circle cx="4" cy="4" r="1.8" />
      <circle cx="12" cy="5" r="1.8" />
      <circle cx="7" cy="12" r="1.8" />
      <path d="M5.6 4.3 10.2 4.8M4.9 5.6 6.3 10.3M11.1 6.6 8 10.6" />
    </>
  ),
  arrow: <path d="M3 8h10M9 4l4 4-4 4" />,
  back: <path d="M13 8H3M7 4 3 8l4 4" />,
  plus: <path d="M8 3v10M3 8h10" />,
  x: <path d="m4 4 8 8M12 4l-8 8" />,
  ext: <path d="M6 3H3v10h10v-3M9 3h4v4M13 3 7 9" />,
  list: <path d="M3 4h10M3 8h10M3 12h10" />,
};

export function Icon({ name, size = 14 }: { name: keyof typeof PATHS | string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="icon"
    >
      {PATHS[name]}
    </svg>
  );
}

export function ErrorBox({ error }: { error: Error }) {
  return (
    <div className="error-box" role="alert">
      {error.message}
    </div>
  );
}
