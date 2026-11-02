# Rebox runbook

v0 runs as **local containers** (Docker Compose). The AWS deployment (Terraform + ECS) is written and statically validated, but it is the **later** target: it has not been applied to a real account yet.

| Piece | Local (now) | AWS (later) |
| --- | --- | --- |
| Postgres 16 | `db` service, volume `pgdata`, host port 54329 | RDS, private subnets |
| Migrations | `migrate` one-shot service | `migrate` ECS task |
| API + explorer | `api` service, http://localhost:3000 | `api` ECS service behind the ALB |
| Worker (runs jobs) | `worker` service | `worker` ECS service |
| Scheduler (enqueues due syncs) | `scheduler` service, loop every 900s | EventBridge Scheduler → `scheduler` task every 15 min |
| Raw store | volume `rawdata` at `/data/raw` (`RAW_STORE=file:///data/raw`) | S3 bucket (`RAW_STORE=s3://<bucket>/raw`) |

Images: `infra/docker/api.Dockerfile` (API + built explorer + `db/` + `psql`, so it also runs migrations) and `infra/docker/worker.Dockerfile` (`rebox-worker`). Both run as non-root and build from the repo root.

---

## Part 1 — Local container stack

### Prerequisites
Docker Engine 24+ with Compose v2 (`docker compose version`). Nothing else is needed: Node, pnpm, Python and uv are only needed for working on the code outside containers (see README).

### First run
```bash
cp .env.example .env
docker compose up --build            # add -d to run in the background
open http://localhost:3000           # explorer; http://localhost:3000/health → {"ok":true,"db":true}
```
Startup order: `db` (healthy) → `migrate` (applies `db/migrations`, seeds the vocabulary, runs `alter role rebox_app login password …`, then exits 0) → `api` (healthy on `/health`) → `worker`; `scheduler` starts once `migrate` succeeds.

`docker compose ps -a` should show `migrate` as `Exited (0)` and the other services `Up`. If `migrate` failed, `api`, `worker` and `scheduler` will not start. Check `docker compose logs migrate`.

Configuration comes from `.env` (Compose reads it for `${...}` substitution):

| Var | Default | Notes |
| --- | --- | --- |
| `INGEST_TOKEN`, `ADMIN_TOKEN` | `dev-ingest-token`, `dev-admin-token` | change them if the machine is shared |
| `APP_DB_PASSWORD` | `rebox_app` | password set on role `rebox_app` by `migrate`; services connect with it |
| `REBOX_ENV` | `dev` | |
| `USER_AGENT` | from `.env` | sent by connectors |
| `GITHUB_TOKEN`, `X_BEARER_TOKEN` | empty | optional; empty disables those live connectors |
| `SCHEDULE_INTERVAL_S` | `900` | scheduler loop period |

Inside the stack, services reach Postgres at `db:5432` and the API at `http://api:3000`. The `localhost` URLs in `.env` (`DATABASE_URL`, `MIGRATE_DATABASE_URL`, `API_URL`) are for running the apps on the host and are ignored by the containers. Variables exported in your shell take precedence over `.env`.

The API port is bound to `127.0.0.1:3000` only. Postgres is published on `localhost:54329` (owner `postgres` / `dev`) for `psql`, `make migrate` and tests.

**Behind a TLS-inspecting proxy** (corporate network or sandbox): build and run with the CA overlay. `EXTRA_CA_FILE` must be a full PEM bundle (system roots plus the proxy CA). The bundle is passed to the build as a BuildKit secret (it is not stored in any image layer) and mounted read-only at run time:
```bash
EXTRA_CA_FILE=/path/to/ca-bundle.pem \
  docker compose -f docker-compose.yml -f infra/docker/compose.extra-ca.yml up --build
```
If Buildx refuses with `additional privileges requested: … --allow=fs.read=…`, also export `BUILDX_BAKE_ENTITLEMENTS_FS=0`.

### Everyday commands
```bash
docker compose up -d --build               # rebuild + restart after pulling new code (migrate re-runs; it is idempotent)
docker compose ps -a
docker compose logs -f api worker          # follow logs; add scheduler / migrate / db as needed
docker compose logs --since 1h worker
docker compose restart worker
docker compose down                        # stop; data volumes are kept
make up / make down                        # Postgres only (for running the apps on the host)
```

### Running a connector manually
```bash
docker compose exec worker rebox-worker sync hn            # run one sync inline now
docker compose exec worker rebox-worker sync hn --limit 50 # small dev run (if the connector supports it)
docker compose exec worker rebox-worker enqueue wikidata   # queue it; the worker picks it up
docker compose exec worker rebox-worker replay hn          # re-extract from the raw store, no fetch
docker compose exec worker rebox-worker schedule-due       # what the scheduler does every 900s
docker compose exec worker rebox-worker status             # connector_state table
```
If the worker container is not running, use `docker compose run --rm worker <cmd>` instead of `exec worker rebox-worker <cmd>`.

### Database access
```bash
docker compose exec db psql -U postgres rebox             # owner (bypasses RLS)
psql postgres://postgres:dev@localhost:54329/rebox        # same, from the host
docker compose run --rm migrate                           # re-run migrations + app role password
```

### Resetting data
```bash
docker compose down -v                     # removes pgdata AND rawdata (all graph data and raw records)
docker compose up --build                  # fresh database, migrated and seeded
```
To reset only the database and keep raw records (and then rebuild the graph with `replay`):
```bash
docker compose stop api worker scheduler
docker compose exec db psql -U postgres -c "drop database rebox with (force)" -c "create database rebox"
docker compose up -d                       # migrate runs again, then the services start
docker compose exec worker rebox-worker replay hn   # one per source
```
`make reset` does the same drop/create + migrate from the host.

### Backups (pg_dump)
```bash
mkdir -p backups
docker compose exec -T db pg_dump -U postgres -Fc rebox > backups/rebox-$(date +%Y%m%d-%H%M).dump
```
Restore into a fresh database:
```bash
docker compose stop api worker scheduler
docker compose exec db psql -U postgres -c "drop database if exists rebox with (force)" -c "create database rebox"
docker compose exec -T db pg_restore -U postgres -d rebox --no-owner < backups/rebox-YYYYMMDD-HHMM.dump
docker compose run --rm migrate            # re-applies grants and the rebox_app password (no-op otherwise)
docker compose up -d
```
Raw records live in the `rawdata` volume. Back them up with:
```bash
docker run --rm -v "$(basename "$PWD")_rawdata:/data:ro" -v "$PWD/backups:/out" busybox tar czf /out/raw-$(date +%Y%m%d).tgz -C /data .
```
(The volume name is `<compose project>_rawdata`; check it with `docker volume ls`.)

### Rotating tokens (local)
Edit `INGEST_TOKEN` / `ADMIN_TOKEN` / `APP_DB_PASSWORD` in `.env`, then `docker compose up -d`. Compose recreates the containers whose environment changed, and `migrate` re-runs and sets the new `rebox_app` password before `api` starts.

### Makefile notes
The Makefile drives host-side development (`make up` starts only `db`; `make api` / `make worker` run the apps on the host against `localhost:54329`). It works unchanged next to the full container stack, with one exception: do not run `make api` while the `api` container holds port 3000.

---

## Part 2 — AWS (later)

Status: Terraform in `infra/terraform/` passes `terraform fmt` and `terraform validate`, and `infra/scripts/*.sh` pass shellcheck. **Nothing has been applied to a real account.** Expect a first-apply fix or two.

### What it creates (one root module, `infra/terraform/*.tf`)
- **network.tf**: VPC across 2 AZs with public and private subnets, one NAT gateway (`enable_nat_gateway`, default true) and an S3 gateway endpoint. Security groups: ALB (80/443 from the internet), api (3000 from the ALB and from tasks), tasks (no inbound), db (5432 from api and tasks). Only the ALB is public.
- **kms.tf**: one customer-managed key (rotation on) used by RDS, S3, Secrets Manager, ECR and CloudWatch Logs.
- **data.tf**: RDS PostgreSQL 16, `db.t4g.micro`, encrypted, private, 7-day backups, `deletion_protection` variable, master password managed by RDS in Secrets Manager.
- **storage.tf**: data bucket (raw records under `raw/`, plus snapshots): private, versioned, SSE-KMS, TLS-only, transition to IA after 30 days.
- **ecr.tf**: `rebox-api` and `rebox-worker` (KMS-encrypted, scan on push, keep the last 30 images).
- **secrets.tf**: `rebox-<env>/INGEST_TOKEN`, `ADMIN_TOKEN` and `APP_DB_PASSWORD` (random), `DATABASE_URL`, and empty placeholders `GITHUB_TOKEN` / `X_BEARER_TOKEN`.
- **ecs.tf / iam.tf**: Fargate cluster; task definitions `api`, `worker`, `scheduler` (`schedule-due`) and `migrate`; services `api` (behind the ALB) and `worker` (1 task); Cloud Map name `api.<env>.rebox.internal`, which the worker uses as `API_URL`; log groups `/rebox/<env>/<task>` with 30-day retention. Per-task execution roles read only that task's secrets. Only the worker task role has AWS permissions (data bucket read/write).
- **alb.tf**: public ALB. HTTP forwards to the api; when `acm_certificate_arn` is set, it adds HTTPS (TLS 1.3 policy) and HTTP redirects to it. Health check `/health`.
- **scheduler.tf**: EventBridge Scheduler `rate(15 minutes)` runs the `scheduler` task.

**How the database URLs work.** Secrets are never plain environment variables in a task definition.
- `DATABASE_URL` (api, worker) is one Secrets Manager secret holding the full URL `postgres://rebox_app:<APP_DB_PASSWORD>@<rds>:5432/rebox?sslmode=require`. Terraform assembles it from the same `random_password` that the migrate task sets on the role. The password is alphanumeric, so it needs no URL encoding, and the apps read `DATABASE_URL` as-is with no entrypoint wrapper.
- `MIGRATE_DATABASE_URL` (migrate) is a plain variable without a password (`postgres://rebox_owner@<rds>/rebox?sslmode=require`). The owner password comes from the RDS-managed master secret as `PGPASSWORD` (ECS `secrets`, JSON key `password`), which libpq picks up. Because of this, RDS rotation of the master password needs no Terraform change.
- `infra/docker/migrate.sh` runs `db/migrate.sh`, then `alter role rebox_app login password :'pw'`, with the password passed as a psql variable so it is quoted safely.

### Prerequisites
AWS CLI v2 with admin-ish credentials for the target account, Terraform ≥ 1.6, Docker with Buildx, `jq`, `git`. The ECS service-linked role must exist (`aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com` once per account; an "already exists" error is fine).

Optional shared state: `infra/scripts/bootstrap-state.sh` creates an S3 state bucket and prints a `backend.hcl`. Copy `infra/terraform/backend.hcl.example` to `backend.hcl`, uncomment `backend "s3" {}` in `versions.tf`, then run `terraform -chdir=infra/terraform init -backend-config=backend.hcl -migrate-state`.

### First deploy
```bash
infra/scripts/deploy.sh                     # optionally: -var acm_certificate_arn=arn:aws:acm:...
```
`deploy.sh` runs `terraform apply` (so the ECR repos exist), logs in to ECR, builds and pushes both images tagged with the git sha (linux/amd64), runs `terraform apply -var image_tag=<sha>`, runs the `migrate` task and waits for exit 0, forces a new deployment of `api` and `worker`, waits for them to stabilize, and prints the URL. It refuses to run with a dirty working tree.

On the very first apply, the services start before any image is pushed and fail until the second apply and redeploy. The circuit breaker may report a failed deployment in that window, which is expected. The RDS instance takes about 10 minutes.

### Redeploy
Commit, then run `infra/scripts/deploy.sh`. Migrations run on every deploy and are idempotent.

### Running a connector manually
```bash
infra/scripts/run-task.sh --wait worker sync hn      # rebox-worker sync hn as a one-off task
infra/scripts/run-task.sh worker status
infra/scripts/run-task.sh --wait migrate
```

### Logs
```bash
aws logs tail /rebox/prod/api --follow
aws logs tail /rebox/prod/worker --since 1h
aws logs tail /rebox/prod/scheduler --since 1h
aws logs tail /rebox/prod/migrate --since 1h
```

### Optional connector tokens
```bash
aws secretsmanager put-secret-value --secret-id rebox-prod/GITHUB_TOKEN --secret-string 'ghp_...'
infra/scripts/deploy.sh -var 'optional_secrets=["GITHUB_TOKEN"]'
```
(An ECS secret reference to a secret with no value makes the task fail to start, which is why tokens are opt-in.)

### Rotating tokens
- `INGEST_TOKEN` / `ADMIN_TOKEN` / `APP_DB_PASSWORD`: `terraform -chdir=infra/terraform apply -replace='random_password.token["ADMIN_TOKEN"]'` (or a different key), then `infra/scripts/deploy.sh`. For `APP_DB_PASSWORD` the `DATABASE_URL` secret updates in the same apply, and deploy runs migrate (which sets the new password) before restarting the services. Expect a few seconds of DB auth errors from running tasks between the migrate run and the restart.
- RDS master password: rotate it in the RDS console or with `aws rds modify-db-instance --rotate-master-user-password`. Nothing else changes.

### Destroying
```bash
terraform -chdir=infra/terraform apply -var db_deletion_protection=false   # first, if protection is on
terraform -chdir=infra/terraform destroy
```
RDS writes a final snapshot `rebox-prod-final`; delete it by hand if you don't need it. Delete objects in the versioned data bucket first, or the destroy will fail on a non-empty bucket. Secrets enter a 7-day recovery window, so recreating the same env within a week needs `aws secretsmanager restore-secret` or a different `env`.

### Cost notes (us-west-2, rough)
Fixed costs, whether or not anyone uses the service:
- NAT gateway: about $33/month plus data processing. Setting `enable_nat_gateway=false` removes it and runs tasks in public subnets with public IPs (still no inbound access).
- ALB: about $16–20/month.
- RDS `db.t4g.micro` + 20 GB gp3: about $15/month.

Variable costs: Fargate (api 0.5 vCPU/1 GB plus worker 0.5 vCPU/1 GB is about $36/month running 24/7, plus short scheduler runs), the KMS key ($1/month), Secrets Manager ($0.40 per secret per month), CloudWatch Logs, S3 and ECR storage.
