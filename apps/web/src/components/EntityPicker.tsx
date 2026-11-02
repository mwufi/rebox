/** Typeahead over /v0/search. Used by the top bar (navigate) and the query builder (pick an id). */
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import type { EntityRef, SearchHit } from '../api';
import { useApi, useDebounced } from '../lib/hooks';
import { KindTag } from './bits';
import { displayName } from '../lib/format';

export interface SuggestProps {
  q: string;
  kind?: string;
  open: boolean;
  onPick: (hit: EntityRef) => void;
  onClose: () => void;
  /** Called with a keyboard handler the input should attach. */
  children: (bind: { onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => void; listId: string; activeId: string | undefined; expanded: boolean }) => React.ReactNode;
  limit?: number;
  /** Enter picks the highlighted hit even without arrow navigation. */
  enterPicks?: boolean;
}

export function Suggest({ q, kind, open, onPick, onClose, children, limit = 8, enterPicks = false }: SuggestProps) {
  const client = useApi();
  const debounced = useDebounced(q.trim(), 140);
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [active, setActive] = useState(0);
  const [navigated, setNavigated] = useState(false);
  const listId = useId();

  useEffect(() => {
    if (!debounced) {
      setHits([]);
      return;
    }
    const ctl = new AbortController();
    client
      .search(debounced, { kind, limit, signal: ctl.signal })
      .then((h) => {
        setHits(h);
        setActive(0);
        setNavigated(false);
      })
      .catch(() => {});
    return () => ctl.abort();
  }, [client, debounced, kind, limit]);

  const visible = open && q.trim() !== '' && hits.length > 0;
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!visible) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setNavigated(true);
      setActive((a) => Math.min(hits.length - 1, a + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setNavigated(true);
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === 'Enter' && hits[active] && (enterPicks || navigated)) {
      // Enter picks the highlighted hit once the user moved through the list (or always, in
      // pickers); otherwise the surrounding form submits the typed text.
      e.preventDefault();
      onPick(hits[active]!);
    } else if (e.key === 'Escape') {
      onClose();
    }
  };

  return (
    <>
      {children({ onKeyDown, listId, activeId: visible ? `${listId}-${active}` : undefined, expanded: visible })}
      {visible && (
        <ul className="suggest" role="listbox" id={listId}>
          {hits.map((h, i) => (
            <li key={h.id} id={`${listId}-${i}`} role="option" aria-selected={i === active} onMouseEnter={() => setActive(i)}>
              <a
                href="#"
                onMouseDown={(e) => {
                  e.preventDefault();
                  onPick(h);
                }}
                onClick={(e) => e.preventDefault()}
              >
                <span className={`elink-name${h.name ? '' : ' unnamed'}`}>{displayName(h)}</span>
                <KindTag kind={h.kind} />
              </a>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/** Picks one entity (by search) and reports its id; shows the chosen entity's name. */
export function EntityPicker({
  value,
  kind,
  onChange,
  placeholder = 'any',
  label,
}: {
  value: string;
  kind?: string;
  onChange: (id: string) => void;
  placeholder?: string;
  label: string;
}) {
  const client = useApi();
  const [q, setQ] = useState('');
  const [open, setOpen] = useState(false);
  const [ref, setRef] = useState<EntityRef | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!value) {
      setRef(null);
      return;
    }
    if (ref?.id === value) return;
    const ctl = new AbortController();
    client
      .entities([value], { signal: ctl.signal })
      .then((r) => setRef(r[0] ?? { id: value, kind: kind ?? 'unknown', name: null }))
      .catch(() => {});
    return () => ctl.abort();
  }, [client, value, kind, ref?.id]);

  if (value) {
    return (
      <div className="picker">
        <div className="picked">
          <span className="elink" title={value}>
            <span className="kind-glyph" data-kind={ref?.kind ?? kind} aria-hidden="true" />
            <span className={`elink-name${ref?.name ? '' : ' unnamed'}`}>{ref ? displayName(ref) : value}</span>
          </span>
          <button
            type="button"
            className="copy-btn"
            aria-label={`Clear ${label}`}
            onClick={() => {
              onChange('');
              setQ('');
              setTimeout(() => inputRef.current?.focus(), 0);
            }}
          >
            ×
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="picker">
      <Suggest
        q={q}
        kind={kind}
        open={open}
        enterPicks
        onClose={() => setOpen(false)}
        onPick={(h) => {
          setRef(h);
          onChange(h.id);
          setOpen(false);
        }}
      >
        {({ onKeyDown, listId, activeId, expanded }) => (
          <input
            ref={inputRef}
            className="field"
            value={q}
            placeholder={placeholder}
            aria-label={label}
            role="combobox"
            aria-controls={listId}
            aria-expanded={expanded}
            aria-activedescendant={activeId}
            aria-autocomplete="list"
            onChange={(e) => {
              setQ(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 120)}
            onKeyDown={(e) => {
              // Never submit an enclosing form from the picker.
              if (e.key === 'Enter') e.preventDefault();
              onKeyDown(e);
            }}
          />
        )}
      </Suggest>
    </div>
  );
}
