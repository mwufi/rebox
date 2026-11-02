#!/usr/bin/env bash
# Create (or recreate) a database, migrate and seed it, and print two URLs:
#   line 1: owner URL (migrations, test setup that must bypass RLS)
#   line 2: app URL   (role rebox_app; what services use; RLS applies)
# Usage: scripts/test-db.sh <dbname>
# Uses PG_ADMIN_URL (default postgres://postgres@localhost:54329/postgres).
set -euo pipefail
name="${1:?usage: scripts/test-db.sh <dbname>}"
admin="${PG_ADMIN_URL:-postgres://postgres@localhost:54329/postgres}"
base="${admin%/*}"
psql "$admin" -q -X -v ON_ERROR_STOP=1 -c "drop database if exists \"$name\" with (force)" -c "create database \"$name\"" >/dev/null 2>&1
DATABASE_URL="$base/$name" "$(cd "$(dirname "$0")/.." && pwd)/db/migrate.sh" >/dev/null
psql "$base/$name" -q -X -c "alter role rebox_app login password 'rebox_app'" >/dev/null
host_part="${base#*://}"; host_part="${host_part#*@}"
echo "$base/$name"
echo "postgres://rebox_app:rebox_app@$host_part/$name"
