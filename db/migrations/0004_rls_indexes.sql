-- Indexes that still work under row-level security.
--
-- Under RLS, Postgres only uses an index condition before the security filter when the
-- operator is leakproof. `value #>> '{}'` (jsonb_extract_path_text) and pg_trgm's `%` are not,
-- so the expression indexes in 0001 can't serve rebox_app queries. Two fixes:
--
-- 1. claim.value_text: a stored copy of the attribute value as text. `=` on text (texteq) is
--    leakproof, so (predicate, value_text) is a real index lookup for hard keys and attr filters.
-- 2. search_name: an RLS-free table of names from PUBLIC layers only (public data needs no RLS),
--    kept in sync by trigger, with a trigram index for fast fuzzy search. Names in private layers
--    are few per viewer and are searched through `claim` (RLS applies).

alter table claim add column value_text text generated always as (value #>> '{}') stored;
drop index if exists claim_value_idx;
drop index if exists claim_name_trgm_idx;
create index claim_value_text_idx on claim (predicate, value_text) where object_id is null;

grant select (value_text) on claim to rebox_app;

create table search_name (
  entity_id  text not null,
  layer_id   text not null,
  name       text not null,
  primary key (entity_id, layer_id)
);
create index search_name_trgm_idx on search_name using gin (name gin_trgm_ops);
grant select, insert, update, delete on search_name to rebox_app;

create function rebox_sync_search_name() returns trigger language plpgsql as $$
begin
  if tg_op in ('UPDATE', 'DELETE') and old.predicate = 'name' and old.layer_id like 'public/%' then
    delete from search_name where entity_id = old.subject and layer_id = old.layer_id;
  end if;
  if tg_op in ('INSERT', 'UPDATE') and new.predicate = 'name' and new.layer_id like 'public/%'
     and not new.retract and new.value_text is not null then
    insert into search_name (entity_id, layer_id, name) values (new.subject, new.layer_id, new.value_text)
    on conflict (entity_id, layer_id) do update set name = excluded.name;
  end if;
  return null;
end $$;

create trigger claim_search_name after insert or update or delete on claim
  for each row execute function rebox_sync_search_name();

-- Backfill (no-op on a fresh database).
insert into search_name (entity_id, layer_id, name)
select subject, layer_id, value_text from claim
where predicate = 'name' and layer_id like 'public/%' and not retract and value_text is not null
on conflict (entity_id, layer_id) do update set name = excluded.name;
