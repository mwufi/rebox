export interface Config {
  databaseUrl: string;
  port: number;
  ingestToken: string | null;
  adminToken: string | null;
  env: 'dev' | 'prod';
}

function token(value: string | undefined): string | null {
  const v = value?.trim();
  return v ? v : null;
}

/** Reads CONTRACTS §9 env vars. Unset tokens disable that principal; REBOX_ENV defaults to prod. */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const databaseUrl = env.DATABASE_URL;
  if (!databaseUrl) throw new Error('DATABASE_URL is required');
  const port = Number(env.PORT ?? 3000);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) throw new Error(`invalid PORT: ${env.PORT}`);
  return {
    databaseUrl,
    port,
    ingestToken: token(env.INGEST_TOKEN),
    adminToken: token(env.ADMIN_TOKEN),
    env: env.REBOX_ENV === 'dev' ? 'dev' : 'prod',
  };
}
