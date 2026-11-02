import type { Db } from './db.js';
import { NO_VIEW } from './db.js';
import type { NormalizerName } from './normalize.js';

export type ValueType = 'text' | 'url' | 'date' | 'int' | 'number' | 'bool' | 'json';

export interface PredicateMeta {
  label?: string;
  value_type?: ValueType;
  identity?: boolean;
  normalize?: NormalizerName;
  filterable?: boolean;
  private?: boolean;
  props?: Record<string, string>;
}

export interface Predicate {
  id: string;
  subject_kinds: string[];
  object_kind: string | null;
  cardinality: 'one' | 'many';
  meta: PredicateMeta;
}

export interface Vocab {
  predicates: ReadonlyMap<string, Predicate>;
  /** Every entity kind the vocabulary mentions. */
  kinds: ReadonlySet<string>;
}

export const isEdge = (p: Predicate): boolean => p.object_kind !== null;

const TTL_MS = 30_000;

export interface VocabCache {
  get(): Promise<Vocab>;
  invalidate(): void;
}

export function createVocabCache(db: Db): VocabCache {
  let cache: { at: number; vocab: Promise<Vocab> } | null = null;

  async function load(): Promise<Vocab> {
    const rows = await db.withView(
      NO_VIEW,
      (tx) => tx<Predicate[]>`select id, subject_kinds, object_kind, cardinality, meta from predicate order by id`,
    );
    const predicates = new Map<string, Predicate>();
    const kinds = new Set<string>();
    for (const r of rows) {
      const p: Predicate = { ...r, meta: r.meta ?? {} };
      predicates.set(p.id, p);
      p.subject_kinds.forEach((k) => kinds.add(k));
      if (p.object_kind) kinds.add(p.object_kind);
    }
    return { predicates, kinds };
  }

  return {
    get() {
      if (cache && Date.now() - cache.at < TTL_MS) return cache.vocab;
      const entry = { at: Date.now(), vocab: load() };
      cache = entry;
      entry.vocab.catch(() => {
        if (cache === entry) cache = null;
      });
      return entry.vocab;
    },
    invalidate() {
      cache = null;
    },
  };
}
