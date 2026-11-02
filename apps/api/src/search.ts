/**
 * Name search (CONTRACTS §2a, §6). Public names come from `search_name` (trigram-indexed, no RLS,
 * public layers only); the viewer's private names come from `claim` (RLS-filtered). Matches map to
 * the canonical (un-merged, visible) entity, so merged-away entities never appear. The displayed
 * name is the viewer's folded name; the score is the best-matching name row of the entity.
 */
import type { Tx } from './db.js';
import { entityRefs } from './entities.js';
import { canonCte, likeEscape, privateVisibleLayers } from './sqlfrag.js';

export interface SearchHit {
  id: string;
  kind: string;
  name: string | null;
  avatar?: string;
  handle?: 'hn' | 'x';
  score: number;
}

export async function search(
  tx: Tx,
  q: string,
  opts: { kind?: string | undefined; limit: number },
): Promise<SearchHit[]> {
  const pattern = '%' + likeEscape(q) + '%';
  const kindFilter = opts.kind ? tx`and e.kind = ${opts.kind}` : tx``;
  // Candidates: word similarity `q <% name` (pg_trgm; finds the query as a word inside longer
  // names, tolerates small typos) or a substring match (prefixes like "strip"). Both are
  // bitmap scans on search_name_trgm_idx.
  // Rank: exact (case-insensitive) match first, then plain similarity (penalizes extra words, so
  // "stripe" beats "stripe press"), then word similarity.
  const rows = await tx<{ id: string; kind: string; score: number }[]>`
    with recursive ${canonCte(tx)},
    hits(entity_id, name) as materialized (
      select s.entity_id, s.name from search_name s
      where (${q} <% s.name or s.name ilike ${pattern}) and s.layer_id = any(rebox_visible())
      union all
      select c.subject, c.value_text from claim c
      where c.layer_id = any(${privateVisibleLayers(tx)}) and c.predicate = 'name' and not c.retract
        and c.value_text is not null
        and (${q} <% c.value_text or c.value_text ilike ${pattern})
    ),
    scored as (
      select coalesce(cn.root, h.entity_id) as id,
             max(case when lower(h.name) = lower(${q}) then 1.0 else 0 end
                 + similarity(h.name, ${q}) * 0.7 + word_similarity(${q}, h.name) * 0.3) as score
      from hits h left join canon cn on cn.member = h.entity_id
      group by 1
    )
    select e.id, e.kind, s.score::float8 as score
    from scored s join entity e on e.id = s.id
    where e.merged_into is null ${kindFilter}
    order by s.score desc, e.id
    limit ${opts.limit}`;
  const refs = await entityRefs(tx, rows.map((r) => r.id));
  return rows.map((r) => {
    const ref = refs.get(r.id);
    return {
      id: r.id,
      kind: r.kind,
      name: ref?.name ?? null,
      ...(ref?.avatar ? { avatar: ref.avatar } : {}),
      ...(ref?.handle ? { handle: ref.handle } : {}),
      score: Math.round(r.score * 1000) / 1000,
    };
  });
}
