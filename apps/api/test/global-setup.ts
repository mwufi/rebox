/**
 * Creates the s1_test database with scripts/test-db.sh (migrations + seed) and hands both URLs to
 * the tests: the owner URL is only for fixture setup that must bypass RLS; the service under test
 * always uses the app URL (role rebox_app, RLS applies).
 */
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { TestProject } from 'vitest/node';

declare module 'vitest' {
  export interface ProvidedContext {
    ownerUrl: string;
    appUrl: string;
  }
}

export default function setup(project: TestProject) {
  const script = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', 'scripts', 'test-db.sh');
  const dbName = process.env.S1_TEST_DB ?? 's1_test';
  const out = execFileSync(script, [dbName], { encoding: 'utf8' }).trim().split('\n');
  const [ownerUrl, appUrl] = out.slice(-2);
  if (!ownerUrl || !appUrl) throw new Error(`test-db.sh printed: ${out.join(' | ')}`);
  project.provide('ownerUrl', ownerUrl);
  project.provide('appUrl', appUrl);
}
