#!/usr/bin/env bash
# Build, push and deploy Rebox to AWS (AWS is the later target; local containers come first).
#   infra/scripts/deploy.sh [extra terraform args...]
# Needs: aws CLI v2 with credentials, docker (buildx), terraform >= 1.6, jq, git.
# Steps: terraform apply (infra) → ECR login → build + push both images tagged with the git sha
#        → terraform apply -var image_tag=<sha> → run migrate and wait → redeploy services → URL.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
tf_dir="$root/infra/terraform"
tf() { terraform -chdir="$tf_dir" "$@"; }
out() { tf output -raw "$1"; }

[ -z "$(git -C "$root" status --porcelain)" ] || { echo "deploy: working tree is dirty; commit first" >&2; exit 1; }
sha="$(git -C "$root" rev-parse --short=12 HEAD)"

echo "deploy: terraform init/apply (infrastructure)"
tf init -input=false >/dev/null
# First run: ECR repos must exist before images can be pushed. Task definitions then point at
# a tag that is not pushed yet, which is harmless because services are redeployed below.
tf apply -input=false -auto-approve -var "image_tag=$sha" "$@"

region="$(out region)"
api_repo="$(out ecr_api)"
worker_repo="$(out ecr_worker)"
registry="${api_repo%%/*}"

echo "deploy: ecr login $registry"
aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"

echo "deploy: build + push $sha"
docker buildx build --platform linux/amd64 -f "$root/infra/docker/api.Dockerfile" -t "$api_repo:$sha" --push "$root"
docker buildx build --platform linux/amd64 -f "$root/infra/docker/worker.Dockerfile" -t "$worker_repo:$sha" --push "$root"

echo "deploy: terraform apply image_tag=$sha"
tf apply -input=false -auto-approve -var "image_tag=$sha" "$@"

echo "deploy: migrate"
"$root/infra/scripts/run-task.sh" --wait migrate

cluster="$(out cluster)"
for svc in api worker; do
  aws ecs update-service --region "$region" --cluster "$cluster" --service "$svc" --force-new-deployment >/dev/null
done
echo "deploy: waiting for services to stabilize"
aws ecs wait services-stable --region "$region" --cluster "$cluster" --services api worker

echo "deploy: done → $(out url)"
