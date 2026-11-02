-- Legacy workers (S17, CONTRACTS §12): give every claim written before worker identities
-- (migration 0005) a worker, so the fold ranks it by its source's trust instead of the 0.5
-- fallback for unattributed claims.
--
-- For each layer source that still has claims with worker_id null, this creates the internal
-- connector `<source>-connector-000000` (source '_' -> '-', like the worker's worker_name()),
-- unless it exists, and attributes those claims (and their old change-feed rows) to it.
-- It is a provenance backfill, not a fact change: the claim_change trigger is disabled for the
-- update, so no change rows are emitted and updated_at is untouched. Idempotent: a second run
-- finds no unattributed claims and changes nothing.
--
-- The worker's created_at is now(), so on an instance where `<source>-connector-<6 digits>`
-- already exists, POST /internal/workers keeps returning that older id (identities are stable);
-- where none exists, the connector's next registration resolves to the legacy worker.
--
-- Trust: the numbers below are config/trust.yaml (sources, else the connector default 0.8);
-- keep them in sync. config.trust_source = 'table' marks the value as the table's, so the
-- worker's registration may update it later (an admin's value is never touched).

create temp table legacy_worker as
select l.source,
       array_agg(l.id order by l.id) as layers,
       trim(both '-' from left(trim(both '-' from regexp_replace(lower(l.source), '[^a-z0-9-]+', '-', 'g')), 40))
         || '-connector-000000' as id,
       (case l.source
          when 'wikidata' then 0.9
          when 'github'   then 0.8
          when 'hn'       then 0.7
          when 'x_spider' then 0.75
          when 'exa'      then 0.7
          else 0.8                          -- defaults.connector
        end)::numeric as trust
from layer l
where exists (select 1 from claim c where c.layer_id = l.id and c.worker_id is null)
group by l.source;

insert into worker (id, kind, source, owner, trust, layers, description, config)
select id, 'connector', source, 'rebox', trust::real, layers,
       'claims written before worker identities (migration 0008)',
       jsonb_build_object('trust_source', 'table', 'trust_table', trust)
from legacy_worker
on conflict (id) do update
  set layers = (select array_agg(distinct x order by x) from unnest(worker.layers || excluded.layers) x)
  where worker.kind = 'connector' and worker.source = excluded.source and worker.owner = 'rebox'
    and not worker.layers @> excluded.layers;

-- Bulk updates: hash joins whatever the (possibly stale) planner statistics say. With a
-- just-truncated or never-analyzed claim table the planner may otherwise pick a nested loop that
-- rescans claim per change row (quadratic: minutes on 50k claims instead of seconds).
set local enable_nestloop = off;

create temp table legacy_claim (id bigint primary key, worker_id text not null);

alter table claim disable trigger claim_change;

with attributed as (
  update claim c
     set worker_id = lw.id
    from layer l
    join legacy_worker lw on lw.source = l.source
    join worker w on w.id = lw.id and w.kind = 'connector' and w.source = lw.source and w.owner = 'rebox'
   where c.worker_id is null
     and c.layer_id = l.id
  returning c.id, c.worker_id
)
insert into legacy_claim select id, worker_id from attributed;

alter table claim enable trigger claim_change;

-- The change rows those claims produced before 0005 name no worker; attribute them too, so the
-- workers page and the timeseries count the legacy workers' claims.
analyze legacy_claim;
update change ch
   set worker_id = lc.worker_id
  from legacy_claim lc
 where ch.claim_id = lc.id
   and ch.worker_id is null;

do $$
declare
  n bigint;
begin
  select count(*) into n from claim where worker_id is null;
  if n > 0 then
    raise warning '0008: % claims still have no worker (a worker id <source>-connector-000000 exists with another kind, source or owner)', n;
  end if;
end $$;

drop table legacy_worker, legacy_claim;
