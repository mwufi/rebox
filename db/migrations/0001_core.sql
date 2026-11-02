-- Rebox core schema: four concepts (layer, predicate, entity, claim) + access tables.
-- See docs/CONTRACTS.md for the rules every service must follow.

create extension if not exists pg_trgm;

-- ── Layer: who can see a set of claims, and where they came from ──────────
-- id is '<scope>/<owner?>/<source>': 'public/wikidata', 'firm/crm', 'user/zen/gmail'
create type scope as enum ('public', 'firm', 'team', 'user');

create table layer (
  id         text primary key,
  scope      scope not null,
  owner      text,                          -- team or user id; null for public and firm
  source     text not null,                 -- connector name: 'wikidata', 'hn', 'github', 'manual'
  status     text not null default 'syncing',
  created_at timestamptz not null default now(),
  check ((scope in ('public','firm')) = (owner is null))
);

-- ── Predicate: the vocabulary. object_kind null = attribute, set = edge ───
create table predicate (
  id            text primary key,           -- 'worked_at', 'name', 'domain'
  subject_kinds text[] not null,            -- any of 'person','company','launch','event','place'
  object_kind   text,                       -- edges only
  cardinality  text not null check (cardinality in ('one', 'many')),
  meta         jsonb not null default '{}'  -- label, value_type, identity, filterable, private
);

-- ── Entity: a thing ──────────────────────────────────────────────────────
create table entity (
  id          text primary key,             -- 'rbx:person:<20 base32>' or 'local:<kind>:<...>'
  kind        text not null,
  home_layer  text not null references layer on delete cascade,
  merged_into text references entity,
  created_at  timestamptz not null default now(),
  check (id ~ '^(rbx|local):[a-z]+:[a-z0-9-]+$')
);
create index entity_kind_idx on entity (kind);
create index entity_merged_into_idx on entity (merged_into) where merged_into is not null;

-- ── Claim: one fact about an entity, in one layer ────────────────────────
create table claim (
  id           bigserial primary key,
  layer_id     text not null references layer on delete cascade,
  subject      text not null references entity on delete cascade,
  predicate    text not null references predicate,
  object_id    text references entity on delete cascade,   -- edges
  value        jsonb,                       -- attributes: a JSON scalar/array; edges: props object or null
  valid_from   timestamptz,
  valid_to     timestamptz,
  retract      boolean not null default false,
  source_ref   text not null,               -- stable upstream id; dedupe key within a layer
  derived_from jsonb,                       -- computed claims: inputs + contributor (hidden from readers)
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

-- Same layer + source_ref + subject + predicate + object = same claim (latest write wins).
create unique index claim_identity_uq
  on claim (layer_id, source_ref, subject, predicate, coalesce(object_id, ''));
create index claim_subject_idx   on claim (subject, predicate);
create index claim_object_idx    on claim (object_id, predicate) where object_id is not null;
create index claim_layer_idx     on claim (layer_id);
create index claim_value_idx     on claim (predicate, (value #>> '{}')) where object_id is null;
create index claim_name_trgm_idx on claim using gin ((value #>> '{}') gin_trgm_ops) where predicate = 'name';

-- ── Access: team membership and sharing ──────────────────────────────────
create table team_member (
  team_id text not null,
  user_id text not null,
  primary key (team_id, user_id)
);

create type share_level as enum ('private', 'existence', 'named', 'full');

create table sharing (
  user_id   text not null,
  source    text not null,
  person_id text references entity on delete cascade,  -- null = default for the source
  level     share_level not null default 'private',
  unique nulls not distinct (user_id, source, person_id)
);

-- ── Row-level security ───────────────────────────────────────────────────
-- Every session/transaction must call rebox_set_view(visible, writable) first.
-- With nothing set, a session sees and writes nothing. FORCE applies RLS to the owner too.
create function rebox_visible() returns text[] language sql stable as $$
  select coalesce(nullif(current_setting('app.visible_layers', true), ''), '{}')::text[]
$$;
create function rebox_writable() returns text[] language sql stable as $$
  select coalesce(nullif(current_setting('app.writable_layers', true), ''), '{}')::text[]
$$;
create function rebox_set_view(visible text[], writable text[] default '{}') returns void
language sql volatile as $$
  select set_config('app.visible_layers', visible::text, true),
         set_config('app.writable_layers', writable::text, true);
$$;

alter table claim  enable row level security;
alter table claim  force  row level security;
alter table entity enable row level security;
alter table entity force  row level security;

create policy claim_read   on claim  for select using (layer_id   = any (rebox_visible()));
create policy claim_insert on claim  for insert with check (layer_id = any (rebox_writable()));
create policy claim_update on claim  for update using (layer_id = any (rebox_writable()))
                                               with check (layer_id = any (rebox_writable()));
create policy claim_delete on claim  for delete using (layer_id = any (rebox_writable()));

create policy entity_read   on entity for select using (home_layer = any (rebox_visible()));
create policy entity_insert on entity for insert with check (home_layer = any (rebox_writable()));
create policy entity_update on entity for update using (home_layer = any (rebox_writable()))
                                                with check (home_layer = any (rebox_writable()));
