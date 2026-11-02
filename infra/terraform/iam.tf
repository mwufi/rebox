data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${var.region}:${local.account_id}:*"]
    }
  }
}

# --- execution roles: pull images, write logs, read exactly the secrets their task injects ---
locals {
  exec_secret_arns = {
    api     = [local.secret_arn.DATABASE_URL, local.secret_arn.INGEST_TOKEN, local.secret_arn.ADMIN_TOKEN]
    worker  = concat([local.secret_arn.DATABASE_URL, local.secret_arn.INGEST_TOKEN], [for s in var.optional_secrets : local.secret_arn[s]])
    migrate = [local.db_master_secret, local.secret_arn.APP_DB_PASSWORD]
  }
}

resource "aws_iam_role" "exec" {
  for_each           = local.exec_secret_arns
  name               = "${local.name}-${each.key}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "exec_managed" {
  for_each   = aws_iam_role.exec
  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "exec_secrets" {
  for_each = local.exec_secret_arns
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = each.value
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "exec_secrets" {
  for_each = local.exec_secret_arns
  name     = "secrets"
  role     = aws_iam_role.exec[each.key].id
  policy   = data.aws_iam_policy_document.exec_secrets[each.key].json
}

# --- task roles: what the running code may call ---
# api and migrate call no AWS APIs; the worker (and scheduler) read/write the data bucket.
resource "aws_iam_role" "task" {
  for_each           = toset(["api", "worker", "migrate"])
  name               = "${local.name}-${each.key}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "worker_s3" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
  }
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.data.arn}/*"]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "worker_s3" {
  name   = "data-bucket"
  role   = aws_iam_role.task["worker"].id
  policy = data.aws_iam_policy_document.worker_s3.json
}
