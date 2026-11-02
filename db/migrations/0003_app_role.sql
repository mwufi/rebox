-- Services connect as rebox_app: not a superuser and no BYPASSRLS, so row-level security applies.
-- Migrations run as the database owner. The deploy sets rebox_app's password out of band:
--   alter role rebox_app login password '...';
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'rebox_app') then
    create role rebox_app nologin nosuperuser nobypassrls;
  end if;
end $$;

grant usage on schema public to rebox_app;
grant select, insert, update, delete on all tables in schema public to rebox_app;
grant usage, select on all sequences in schema public to rebox_app;
grant execute on all functions in schema public to rebox_app;
alter default privileges in schema public grant select, insert, update, delete on tables to rebox_app;
alter default privileges in schema public grant usage, select on sequences to rebox_app;
alter default privileges in schema public grant execute on functions to rebox_app;

-- Readers never see who contributed a derived claim.
-- (A table-level SELECT would override a column revoke, so drop it and grant columns explicitly.)
revoke select on claim from rebox_app;
grant select (id, layer_id, subject, predicate, object_id, value, valid_from, valid_to, retract,
              source_ref, created_at, updated_at) on claim to rebox_app;
