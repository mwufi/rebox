/**
 * Generates or refreshes wikis for the N most-connected public entities.
 *   pnpm --filter @rebox/api wiki:batch -- --top 20 [--kind company] [--force] [--dry-run]
 * Env: DATABASE_URL (rebox_app), ANTHROPIC_API_KEY, WIKI_MODEL. Skips fresh articles; paces
 * calls with the same limiter as the API (WIKI_RATE_PER_MIN, default 6) by waiting, not failing.
 */
import { createDb } from '../db.js';
import { createVocabCache } from '../vocab.js';
import { wikiSettingsFromEnv } from './model.js';
import { createLimiter, createWikiService, publicView, RateLimited } from './service.js';

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i > 0 ? process.argv[i + 1] : undefined;
}
const flag = (name: string) => process.argv.includes(`--${name}`);

const out = (line: Record<string, unknown>) => process.stdout.write(`${JSON.stringify(line)}\n`);

async function main() {
  const url = process.env.DATABASE_URL;
  if (!url) throw new Error('DATABASE_URL is required');
  const top = Number(arg('top') ?? 10);
  if (!Number.isInteger(top) || top < 1 || top > 10_000) throw new Error('--top must be an integer in 1..10000');
  const kind = arg('kind');
  const dry = flag('dry-run');
  const settings = wikiSettingsFromEnv();
  if (!settings.model && !dry) throw new Error('ANTHROPIC_API_KEY is required (or pass --dry-run)');

  const db = createDb(url, { max: 2 });
  try {
    // Most connected = distinct visible neighbors over public edges, either direction.
    const ranked = await db.withView(await publicView(db), (tx) => tx<{ id: string; kind: string; degree: number }[]>`
      with edges as (
        select c.subject as id, c.object_id as other from claim c where c.object_id is not null
        union
        select c.object_id, c.subject from claim c where c.object_id is not null
      )
      select e.id, e.kind, count(distinct ed.other)::int as degree
      from edges ed join entity e on e.id = ed.id
      where e.merged_into is null ${kind ? tx`and e.kind = ${kind}` : tx``}
      group by e.id, e.kind
      order by degree desc, e.id
      limit ${top}`);
    out({ msg: 'ranked', n: ranked.length, top: ranked.slice(0, 5) });
    if (dry || !settings.model) return;

    const limiter = createLimiter(settings.rateLimit.max, settings.rateLimit.windowMs);
    const service = createWikiService({ db, vocab: createVocabCache(db), model: settings.model, limiter, log: out });
    let made = 0;
    let fresh = 0;
    let failed = 0;
    for (const r of ranked) {
      for (;;) {
        try {
          const before = await service.get(r.id);
          const w = await service.generate(r.id, { force: flag('force'), by: 'wiki:batch' });
          if (w === 'missing') break;
          if (before && before !== 'missing' && before.generated_at === w.generated_at) fresh += 1;
          else made += 1;
          out({ id: r.id, kind: r.kind, degree: r.degree, generated_at: w.generated_at, stale: w.stale });
          break;
        } catch (err) {
          if (err instanceof RateLimited) {
            await new Promise((res) => setTimeout(res, err.retryAfterS * 1000));
            continue;
          }
          failed += 1;
          out({ id: r.id, level: 'error', error: String(err) });
          break;
        }
      }
    }
    out({ msg: 'done', generated: made, fresh, failed });
  } finally {
    await db.close();
  }
}

main().catch((err) => {
  out({ level: 'error', error: String(err) });
  process.exit(1);
});
