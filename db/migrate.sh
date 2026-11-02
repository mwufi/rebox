#!/usr/bin/env bash
# Apply db/migrations/*.sql in order (each once, in a transaction), then db/seed/*.sql (idempotent).
# Usage: DATABASE_URL=postgres://... db/migrate.sh [--no-seed]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
: "${DATABASE_URL:?DATABASE_URL is required}"
psql_() { psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -X "$@"; }

psql_ -c "create table if not exists schema_migrations (version text primary key, applied_at timestamptz not null default now())"

for f in "$here"/migrations/*.sql; do
  v="$(basename "$f" .sql)"
  if [ -z "$(psql_ -tA -c "select 1 from schema_migrations where version = '$v'")" ]; then
    echo "migrate: applying $v"
    psql_ -1 -f "$f" -c "insert into schema_migrations (version) values ('$v')"
  fi
done

if [ "${1:-}" != "--no-seed" ]; then
  for f in "$here"/seed/*.sql; do
    echo "migrate: seeding $(basename "$f")"
    psql_ -1 -f "$f"
  done
fi
echo "migrate: done"
