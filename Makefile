# Load .env if present
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: up down migrate api web worker sync replay test reset

up:        ## start Postgres in Docker
	docker compose up -d --wait db

down:
	docker compose down

migrate:   ## schema + seed + app role password (dev)
	DATABASE_URL=$(MIGRATE_DATABASE_URL) db/migrate.sh
	psql $(MIGRATE_DATABASE_URL) -q -c "alter role rebox_app login password 'rebox_app'"

api:
	pnpm --filter @rebox/api dev

web:
	pnpm --filter @rebox/web dev

worker:
	cd apps/worker && uv run rebox-worker run

sync:      ## make sync SOURCE=hn
	cd apps/worker && uv run rebox-worker sync $(SOURCE)

replay:    ## make replay SOURCE=hn
	cd apps/worker && uv run rebox-worker replay $(SOURCE)

test:
	pnpm -r test
	cd apps/worker && uv run pytest -q

reset:     ## drop and recreate the dev database
	psql $(MIGRATE_DATABASE_URL:/rebox=/postgres) -c "drop database if exists rebox with (force)" -c "create database rebox"
	$(MAKE) migrate
