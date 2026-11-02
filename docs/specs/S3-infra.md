# S3 — Infra: containers, AWS (Terraform), CI

**Goal:** everything needed to deploy Rebox Public to an empty AWS account with one script, once credentials exist. No AWS credentials are available in this environment, so verification is static (fmt, validate, lint, container builds where possible).

**Branch:** `s3-infra`  **DB prefix:** `s3_`

## Owns
`infra/**`, `.github/workflows/**`, `docs/RUNBOOK.md`, `.dockerignore`.

## Build

1. **Dockerfiles** (`infra/docker/`):
   - `api.Dockerfile`: multi-stage; installs pnpm deps for `apps/api` and `apps/web`, builds the web app into `apps/api/public` (skip gracefully if `apps/web` has no build script yet), builds the API, final stage `node:22-slim` with `postgresql-client` and `db/` copied in so the same image can run migrations (`db/migrate.sh`). Runs as non-root, `CMD ["node","dist/server.js"]`.
   - `worker.Dockerfile`: `python:3.13-slim` + uv, installs `apps/worker`, non-root, `ENTRYPOINT ["rebox-worker"]`, default `CMD ["run"]`.
   - The app slices are being written in parallel, so write Dockerfiles against the conventions in CONTRACTS §10 (package names `@rebox/api`, `@rebox/web`, console script `rebox-worker`).
2. **Terraform** (`infra/terraform/`), AWS provider ~> 5, single root module with small files, `us-west-2` default, `env` variable (`prod`):
   - network: VPC, 2 AZs, public + private subnets, one NAT gateway (variable to disable), S3 gateway VPC endpoint.
   - kms: one customer-managed key for RDS, S3, Secrets Manager, logs.
   - data: RDS PostgreSQL 16 (`db.t4g.micro` default, encrypted, private, `manage_master_user_password = true`, backups 7 days, deletion protection variable).
   - storage: S3 bucket for raw records + snapshots (private, versioned, SSE-KMS, lifecycle to IA after 30 days).
   - ecr: repos `rebox-api`, `rebox-worker`.
   - secrets: Secrets Manager entries for `INGEST_TOKEN`, `ADMIN_TOKEN` (random), `APP_DB_PASSWORD` (random, used for role `rebox_app`), optional `GITHUB_TOKEN` (placeholder, set out of band).
   - ecs: cluster; task definitions `api`, `worker`, `migrate` (api image, command `db/migrate.sh` then sets the `rebox_app` password), `scheduler` (worker image, command `schedule-due`); services `api` (behind ALB, health check `/health`) and `worker` (desired count 1); IAM task roles with least privilege (worker: S3 raw bucket rw; both: read their secrets); CloudWatch log groups (30 days).
   - alb: public ALB, HTTP listener; HTTPS listener + redirect when `acm_certificate_arn` is set.
   - scheduler: EventBridge Scheduler `rate(15 minutes)` running the `scheduler` task.
   - outputs: ALB URL, ECR repo URLs, cluster name, subnet/security group ids needed by `run-task`.
   - backend: local by default; `backend.hcl.example` for S3 state.
   - `DATABASE_URL` for tasks: build from RDS endpoint + `rebox_app` + secret password; `MIGRATE_DATABASE_URL` from the RDS-managed master secret. Document the approach.
3. **Scripts** (`infra/scripts/`): `deploy.sh` (ecr login → build + push both images tagged with git sha → `terraform apply -var image_tag=<sha>` → run `migrate` task and wait → force new deployment of services → print URL); `run-task.sh <task> [cmd...]` helper; `bootstrap-state.sh` (optional S3 state bucket).
4. **CI** (`.github/workflows/ci.yml`): on PR/push: Postgres 16 service container; api typecheck + tests; worker ruff + pytest; terraform fmt + validate. Tolerate missing app dirs (they land in parallel) by checking for `package.json` / `pyproject.toml`.
5. **docs/RUNBOOK.md**: prerequisites, first deploy, redeploy, running a connector manually, viewing logs, rotating tokens, destroying, cost notes (NAT gateway + ALB + RDS are the fixed costs).

## Verification
- Install Terraform locally into `infra/.bin/` (download from releases.hashicorp.com; do not commit the binary). `terraform fmt -check -recursive` and `terraform init -backend=false && terraform validate` pass in `infra/terraform`.
- `bash -n` on every script; `shellcheck` if installable via pip (`shellcheck-py`).
- Docker: the daemon is not running in this sandbox. Try `dockerd` in the background; if it starts, build both images (api build may fail until the api slice merges; report). If it cannot start, validate Dockerfiles with `hadolint` if installable, and say so in the report.
- `actionlint` on the workflow if installable (pip `actionlint-py`).
