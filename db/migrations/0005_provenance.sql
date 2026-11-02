-- Provenance and observability: every write comes from a named worker in a run,
-- claims can carry evidence, spiders keep a discovery frontier, and Grafana reads
-- operational tables through a read-only role. See CONTRACTS §12.

-- ── Worker: a named, accountable writer (internal spider or external contributor) ──
create table worker (
  id           text primary key check (id ~ '^[a-z0-9][a-z0-9-]{2,62}$'),  -- 'dbooth-spider-292929'
  kind         text not null check (kind in ('connector','spider','parser','enricher','external')),
  source       text not null,                  -- connector name it runs: 'x_spider', 'hn', 'x_parse'
  owner        text not null default 'rebox',  -- 'rebox' = internal; otherwise the contributor
  trust        real not null default 0.5 check (trust >= 0 and trust <= 1),
  layers       text[] not null,                -- layers it may write
  token_hash   text,                           -- sha256 hex of an external worker's token
  description  text,
  config       jsonb not null default '{}',    -- e.g. {"seeds": ["davidbooth"]}
  status       text not null default 'active' check (status in ('active','paused','revoked')),
  created_at   timestamptz not null default now(),
  last_seen_at timestamptz
);

-- ── Run: one execution of a worker (a sync, replay, parse or enrich pass) ──
create table run (
  id          bigserial primary key,
  worker_id   text not null references worker,
  source      text not null,
  kind        text not null check (kind in ('sync','replay','parse','enrich','wiki')),
  status      text not null default 'running' check (status in ('running','ok','failed')),
  started_at  timestamptz not null default now(),
  finished_at timestamptz,
  stats       jsonb not null default '{}',  -- fetched, claims, written, minted, matched, review, rejected, cost_usd, api_calls
  error       text
);
create index run_worker_idx on run (worker_id, started_at desc);

-- ── Claims carry who wrote them and why we believe them ──
alter table claim add column worker_id text references worker;
alter table claim add column run_id    bigint references run;
alter table claim add column evidence  jsonb;   -- [{"url","author","quote","observed_at"}]
create index claim_worker_idx on claim (worker_id);
grant select (worker_id, run_id, evidence) on claim to rebox_app;

alter table change add column worker_id text;
alter table change add column run_id    bigint;
create index change_worker_at_idx on change (worker_id, at);
create index change_at_idx on change (at);

create or replace function rebox_claim_change() returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    insert into change (op, layer_id, claim_id, subject, predicate, object_id, worker_id, run_id)
    values ('delete', old.layer_id, old.id, old.subject, old.predicate, old.object_id, old.worker_id, old.run_id);
    return old;
  end if;
  insert into change (op, layer_id, claim_id, subject, predicate, object_id, worker_id, run_id)
  values (lower(tg_op), new.layer_id, new.id, new.subject, new.predicate, new.object_id, new.worker_id, new.run_id);
  return new;
end $$;

-- ── Frontier: what a spider has discovered but not yet indexed ──
create table frontier (
  source        text not null,          -- spider name: 'x_spider', 'exa'
  key           text not null,          -- upstream id: X user id, domain, URL
  kind          text not null,          -- expected entity kind
  label         text,                   -- handle or name, for humans
  score         real not null default 0,
  depth         int  not null default 0,
  discovered_by text[] not null default '{}',   -- seeds / workers that surfaced it
  status        text not null default 'new' check (status in ('new','fetched','skipped','failed')),
  payload       jsonb,                  -- whatever discovery already knows (e.g. the X user object)
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  fetched_at    timestamptz,
  primary key (source, key)
);
create index frontier_queue_idx on frontier (source, score desc) where status = 'new';

-- ── Wiki: generated articles, derived from public facts only (a cache, not a claim) ──
create table entity_wiki (
  entity_id    text primary key,
  content_md   text not null,
  model        text not null,
  input_seq    bigint not null,         -- change seq the article was generated from
  sources      jsonb not null default '[]',
  generated_at timestamptz not null default now(),
  generated_by text not null            -- worker id or 'api'
);

grant select, insert, update, delete on worker, run, frontier, entity_wiki to rebox_app;
grant usage, select on sequence run_id_seq to rebox_app;

-- ── Read-only metrics role for Grafana: operational tables only, never claim/entity ──
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'rebox_metrics') then
    create role rebox_metrics nologin nosuperuser nobypassrls;
  end if;
end $$;
grant usage on schema public to rebox_metrics;
grant select on change, run, job, connector_state, review_item, merge_log, frontier, raw_record, layer, predicate
  to rebox_metrics;
grant select (id, kind, source, owner, trust, layers, description, config, status, created_at, last_seen_at)
  on worker to rebox_metrics;   -- never token_hash
