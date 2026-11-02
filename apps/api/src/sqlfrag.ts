import type { Tx } from './db.js';

/** Escapes LIKE/ILIKE metacharacters so user text matches literally (default escape char is \). */
export function likeEscape(s: string): string {
  return s.replace(/[\\%_]/g, (m) => `\\${m}`);
}

/**
 * CTE body `merged(root, member, depth)`: every merged-away entity paired with each entity above
 * it in its merged_into chain. Merges are rare, so this stays small. The depth cap makes a corrupt
 * cycle terminate. Use inside `with recursive`.
 */
export function mergedMembersCte(tx: Tx) {
  return tx`
    merged(root, member, depth) as (
      select merged_into, id, 1 from entity where merged_into is not null
      union all
      select e.merged_into, m.member, m.depth + 1
      from merged m join entity e on e.id = m.root
      where e.merged_into is not null and m.depth < 32
    )`;
}

/**
 * CTE bodies `merged` + `canon(member, root)`: each merged-away entity mapped to the final,
 * un-merged (and visible) entity its chain ends at. Use inside `with recursive`.
 */
export function canonCte(tx: Tx) {
  return tx`
    ${mergedMembersCte(tx)},
    canon(member, root) as (
      select m.member, m.root from merged m join entity r on r.id = m.root where r.merged_into is null
    )`;
}
