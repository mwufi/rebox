-- Operational tables: change feed, job queue, connector state, raw record index,
-- resolution log and review queue. None of these hold graph facts; no RLS.

-- ── Change feed (outbox): every claim insert/update/delete, in commit order ─
create table change (
  seq        bigserial primary key,
  op         text not null check (op in ('insert', 'update', 'delete', 'merge', 'split')),
  layer_id   text,
  claim_id   bigint,
  subject    text,
  predicate  text,
  object_id  text,
  at         timestamptz not null default now()
);
create index change_layer_seq_idx on change (layer_id, seq);

create function rebox_claim_change() returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    insert into change (op, layer_id, claim_id, subject, predicate, object_id)
    values ('delete', old.layer_id, old.id, old.subject, old.predicate, old.object_id);
    return old;
  end if;
  insert into change (op, layer_id, claim_id, subject, predicate, object_id)
  values (lower(tg_op), new.layer_id, new.id, new.subject, new.predicate, new.object_id);
  return new;
end $$;

create trigger claim_change after insert or update or delete on claim
  for each row execute function rebox_claim_change();

-- ── Job queue (worker): claim with FOR UPDATE SKIP LOCKED ────────────────
create table job (
  id          bigserial primary key,
  kind        text not null,                 -- 'sync' | 'replay'
  payload     jsonb not null default '{}',   -- e.g. {"source": "wikidata"}
  status      text not null default 'queued' check (status in ('queued','running','done','failed')),
  run_after   timestamptz not null default now(),
  attempts    int not null default 0,
  max_attempts int not null default 3,
  last_error  text,
  locked_at   timestamptz,
  created_at  timestamptz not null default now(),
  finished_at timestamptz
);
create index job_ready_idx on job (run_after) where status = 'queued';

-- ── Connector state: cursor + schedule per source ────────────────────────
create table connector_state (
  source       text primary key,
  cursor       jsonb,
  interval_s   int not null default 86400,
  next_run_at  timestamptz not null default now(),
  last_run_at  timestamptz,
  last_status  text,
  last_stats   jsonb
);

-- ── Raw record index: what was fetched, where it is stored ───────────────
create table raw_record (
  source       text not null,
  external_id  text not null,
  content_hash text not null,
  store_key    text not null,               -- key inside RAW_STORE
  fetched_at   timestamptz not null,
  primary key (source, external_id)
);

-- ── Resolution: every decision is logged; ambiguous ones go to review ───
create table resolution_log (
  id          bigserial primary key,
  source      text not null,
  source_ref  text not null,
  kind        text not null,
  identity    jsonb not null,                -- identity keys + name hints the resolver saw
  decision    text not null check (decision in ('matched','minted','review')),
  entity_id   text,
  confidence  real not null,
  evidence    jsonb not null default '[]',
  created_at  timestamptz not null default now()
);
create index resolution_log_entity_idx on resolution_log (entity_id);

create table review_item (
  id          bigserial primary key,
  kind        text not null,                 -- 'ambiguous_match' | 'possible_duplicate'
  entity_ids  text[] not null,               -- candidates
  reason      text not null,
  evidence    jsonb not null default '[]',
  status      text not null default 'open' check (status in ('open','merged','dismissed')),
  resolved_by text,
  created_at  timestamptz not null default now(),
  resolved_at timestamptz
);
create index review_item_open_idx on review_item (created_at) where status = 'open';

create table merge_log (
  id          bigserial primary key,
  op          text not null check (op in ('merge','split')),
  from_id     text not null,
  into_id     text,
  actor       text not null,
  at          timestamptz not null default now()
);
