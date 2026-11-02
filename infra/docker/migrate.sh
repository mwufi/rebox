#!/usr/bin/env bash
# One-shot migration job, run from the api image (compose `migrate` service, ECS `migrate` task).
#   1. db/migrate.sh as the database owner (MIGRATE_DATABASE_URL)
#   2. let role rebox_app log in with APP_DB_PASSWORD
# The password is passed as a psql variable (:'pw' quotes it safely); psql only expands
# variables in script input, so the statement is fed on stdin, not with -c.
# Libpq env vars (PGPASSWORD, PGSSLMODE, ...) apply, so the owner password may come from PGPASSWORD.
set -euo pipefail
: "${MIGRATE_DATABASE_URL:?MIGRATE_DATABASE_URL is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"
here="$(cd "$(dirname "$0")/../.." && pwd)"

DATABASE_URL="$MIGRATE_DATABASE_URL" "$here/db/migrate.sh" "$@"

echo "alter role rebox_app login password :'pw';" \
  | psql "$MIGRATE_DATABASE_URL" -v ON_ERROR_STOP=1 -q -X -v pw="$APP_DB_PASSWORD"
echo "migrate: rebox_app login enabled"
