output "url" {
  value = "${local.https ? "https" : "http"}://${aws_lb.main.dns_name}"
}

output "ecr_api" {
  value = aws_ecr_repository.this["rebox-api"].repository_url
}

output "ecr_worker" {
  value = aws_ecr_repository.this["rebox-worker"].repository_url
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "task_families" {
  value = { for k, t in aws_ecs_task_definition.this : k => t.family }
}

# Used by infra/scripts/run-task.sh.
output "task_subnet_ids" {
  value = local.task_subnet_ids
}

output "tasks_security_group_id" {
  value = aws_security_group.tasks.id
}

output "assign_public_ip" {
  value = local.assign_public_ip
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "region" {
  value = var.region
}
