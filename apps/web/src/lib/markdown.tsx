/**
 * A deliberately small Markdown renderer for generated wiki articles: paragraphs, `##`/`###`
 * headings, `-`/`*` lists, **bold**, *italic*, `code`, [links](https://…) and footnotes
 * ([^1] references, `[^1]: …` definitions). Output is React elements only (never innerHTML),
 * and links are restricted to http(s), so model output cannot inject markup or script URLs.
 */
import { Fragment, type ReactNode } from 'react';

export interface Footnote {
  n: string;
  body: string;
}

export interface ParsedDoc {
  blocks: Block[];
  footnotes: Footnote[];
}

type Block = { type: 'p'; text: string } | { type: 'h'; level: 2 | 3; text: string } | { type: 'ul'; items: string[] };

export function parseMarkdown(md: string): ParsedDoc {
  const footnotes: Footnote[] = [];
  const blocks: Block[] = [];
  let para: string[] = [];
  let list: string[] | null = null;
  const flush = () => {
    if (para.length) blocks.push({ type: 'p', text: para.join(' ') });
    para = [];
    if (list) blocks.push({ type: 'ul', items: list });
    list = null;
  };
  for (const raw of md.replace(/\r\n?/g, '\n').split('\n')) {
    const line = raw.trimEnd();
    const fn = /^\[\^([\w-]+)\]:\s*(.*)$/.exec(line.trim());
    if (fn) {
      flush();
      footnotes.push({ n: fn[1]!, body: fn[2]! });
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line.trim());
    if (h) {
      flush();
      blocks.push({ type: 'h', level: h[1]!.length <= 2 ? 2 : 3, text: h[2]!.replace(/\s*#+\s*$/, '') });
      continue;
    }
    const li = /^\s*[-*]\s+(.*)$/.exec(line);
    if (li) {
      if (para.length) {
        blocks.push({ type: 'p', text: para.join(' ') });
        para = [];
      }
      (list ??= []).push(li[1]!);
      continue;
    }
    if (!line.trim()) {
      flush();
      continue;
    }
    if (list) {
      blocks.push({ type: 'ul', items: list });
      list = null;
    }
    para.push(line.trim());
  }
  flush();
  return { blocks, footnotes };
}

const SAFE_URL = /^https?:\/\/[^\s<>"]+$/i;

/** Inline markup → React nodes. `fnHref` maps a footnote label to its anchor id. */
export function renderInline(text: string, opts: { fnId: (n: string) => string; refId?: (n: string, i: number) => string | undefined }): ReactNode[] {
  const out: ReactNode[] = [];
  // Order matters: footnote refs before links ([^1] would otherwise look like a link label).
  const re = /\[\^([\w-]+)\]|\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|`([^`]+)`/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const key = `${m.index}`;
    if (m[1] !== undefined) {
      const n = m[1];
      out.push(
        <sup key={key} className="fn-ref" id={opts.refId?.(n, i++)}>
          <a href={`#${opts.fnId(n)}`} aria-label={`Source ${n}`}>
            {n}
          </a>
        </sup>,
      );
    } else if (m[2] !== undefined) {
      const url = m[3]!;
      out.push(
        SAFE_URL.test(url) ? (
          <a key={key} href={url} target="_blank" rel="noopener noreferrer nofollow">
            {m[2]}
          </a>
        ) : (
          <Fragment key={key}>{m[2]}</Fragment>
        ),
      );
    } else if (m[4] !== undefined) out.push(<strong key={key}>{m[4]}</strong>);
    else if (m[5] !== undefined) out.push(<em key={key}>{m[5]}</em>);
    else if (m[6] !== undefined) out.push(<code key={key}>{m[6]}</code>);
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source, idPrefix = 'md' }: { source: string; idPrefix?: string }) {
  const doc = parseMarkdown(source);
  const fnId = (n: string) => `${idPrefix}-fn-${n}`;
  const refId = (n: string) => `${idPrefix}-ref-${n}`;
  const seen = new Set<string>();
  // Only the first reference to a footnote gets the back-link anchor.
  const firstRef = (n: string) => {
    if (seen.has(n)) return undefined;
    seen.add(n);
    return refId(n);
  };
  const inline = (t: string) => renderInline(t, { fnId, refId: (n) => firstRef(n) });
  return (
    <div className="md">
      {doc.blocks.map((b, i) =>
        b.type === 'p' ? (
          <p key={i}>{inline(b.text)}</p>
        ) : b.type === 'h' ? (
          b.level === 2 ? (
            <h3 key={i}>{inline(b.text)}</h3>
          ) : (
            <h4 key={i}>{inline(b.text)}</h4>
          )
        ) : (
          <ul key={i}>
            {b.items.map((it, j) => (
              <li key={j}>{inline(it)}</li>
            ))}
          </ul>
        ),
      )}
      {doc.footnotes.length > 0 && (
        <ol className="footnotes" aria-label="Sources">
          {doc.footnotes.map((f) => (
            <li key={f.n} id={fnId(f.n)} value={/^\d+$/.test(f.n) ? Number(f.n) : undefined}>
              {renderInline(f.body, { fnId })}{' '}
              <a href={`#${refId(f.n)}`} className="fn-back" aria-label="Back to text">
                ↩
              </a>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
