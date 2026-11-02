import postgres from 'postgres';

export type Tx = postgres.TransactionSql;
export type Sql = postgres.Sql;
/** A composable SQL fragment (tx`...` nested inside another tx`...`). */
export type Frag = postgres.PendingQuery<postgres.Row[]>;

/** Which layers a transaction may read and write (CONTRACTS §2). */
export interface View {
  visible: readonly string[];
  writable: readonly string[];
  /**
   * Also make every public layer visible, read from the `layer` table inside the same
   * rebox_set_view statement (never stale). The writer uses this; reads use the 30s cache.
   */
  allPublic?: boolean;
}

export const NO_VIEW: View = { visible: [], writable: [] };

export interface Db {
  readonly sql: Sql;
  /** Runs fn in a transaction whose first statement is rebox_set_view(visible, writable). */
  withView<T>(view: View, fn: (tx: Tx) => Promise<T>): Promise<T>;
  /** Ids of all public layers (cached 30s). */
  publicLayers(): Promise<string[]>;
  invalidatePublicLayers(): void;
  ping(): Promise<boolean>;
  close(): Promise<void>;
}

const PUBLIC_LAYERS_TTL_MS = 30_000;

export interface DbOptions {
  max?: number;
  /** Called with every statement's SQL and parameters (tests use it to EXPLAIN real queries). */
  onQuery?: (query: string, params: unknown[]) => void;
}

export function createDb(url: string, opts: DbOptions = {}): Db {
  const sql = postgres(url, {
    max: opts.max ?? 10,
    idle_timeout: 30,
    onnotice: () => {},
    ...(opts.onQuery ? { debug: (_conn: number, query: string, params: unknown[]) => opts.onQuery!(query, params) } : {}),
    // Date-only valid_from values must mean UTC midnight regardless of the server's TimeZone.
    connection: { TimeZone: 'UTC', application_name: 'rebox-api' },
  });

  async function withView<T>(view: View, fn: (tx: Tx) => Promise<T>): Promise<T> {
    const result = await sql.begin(async (tx) => {
      const visible = view.allPublic
        ? tx`array(select id from layer where scope = 'public') || ${[...view.visible]}::text[]`
        : tx`${[...view.visible]}::text[]`;
      await tx`select rebox_set_view(${visible}, ${[...view.writable]}::text[])`;
      return fn(tx);
    });
    return result as T;
  }

  let cache: { at: number; ids: Promise<string[]> } | null = null;
  function publicLayers(): Promise<string[]> {
    if (cache && Date.now() - cache.at < PUBLIC_LAYERS_TTL_MS) return cache.ids;
    const ids = withView(NO_VIEW, async (tx) => {
      const rows = await tx<{ id: string }[]>`select id from layer where scope = 'public' order by id`;
      return rows.map((r) => r.id);
    });
    const entry = { at: Date.now(), ids };
    cache = entry;
    ids.catch(() => {
      if (cache === entry) cache = null;
    });
    return ids;
  }

  return {
    sql,
    withView,
    publicLayers,
    invalidatePublicLayers: () => {
      cache = null;
    },
    ping: async () => {
      try {
        await withView(NO_VIEW, (tx) => tx`select 1`);
        return true;
      } catch {
        return false;
      }
    },
    close: () => sql.end({ timeout: 5 }),
  };
}
