#!/usr/bin/env bash
# Run a one-off ECS task from a Rebox task definition, optionally overriding its command.
#   infra/scripts/run-task.sh [--wait] <api|worker|migrate|scheduler> [cmd...]
# Examples:
#   infra/scripts/run-task.sh --wait migrate
#   infra/scripts/run-task.sh --wait worker sync hn        # rebox-worker sync hn
#   infra/scripts/run-task.sh worker status
# With --wait, waits for the task to stop and exits with the container's exit code.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
tf() { terraform -chdir="$root/infra/terraform" output "$@"; }

wait=false
if [ "${1:-}" = "--wait" ]; then wait=true; shift; fi
task="${1:?usage: run-task.sh [--wait] <api|worker|migrate|scheduler> [cmd...]}"; shift

region="$(tf -raw region)"
cluster="$(tf -raw cluster)"
family="$(tf -json task_families | jq -er --arg t "$task" '.[$t]')"
subnets="$(tf -json task_subnet_ids | jq -r 'join(",")')"
sg="$(tf -raw tasks_security_group_id)"
public_ip="$( [ "$(tf -raw assign_public_ip)" = "true" ] && echo ENABLED || echo DISABLED )"

overrides='{}'
if [ "$#" -gt 0 ]; then
  overrides="$(jq -cn --arg n "$task" '{containerOverrides: [{name: $n, command: $ARGS.positional}]}' --args "$@")"
fi

arn="$(aws ecs run-task --region "$region" --cluster "$cluster" --task-definition "$family" \
  --launch-type FARGATE --count 1 --started-by run-task.sh \
  --network-configuration "awsvpcConfiguration={subnets=[$subnets],securityGroups=[$sg],assignPublicIp=$public_ip}" \
  --overrides "$overrides" --query 'tasks[0].taskArn' --output text)"
echo "run-task: started $arn"
echo "run-task: logs: aws logs tail /rebox/$(tf -raw cluster | sed 's/^rebox-//')/$task --follow --region $region"

if $wait; then
  aws ecs wait tasks-stopped --region "$region" --cluster "$cluster" --tasks "$arn"
  code="$(aws ecs describe-tasks --region "$region" --cluster "$cluster" --tasks "$arn" \
    --query 'tasks[0].containers[0].exitCode' --output text)"
  echo "run-task: $task exited with $code"
  [ "$code" = "0" ]
fi
