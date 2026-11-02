# Secrets Manager entries, all encrypted with the CMK. Tasks receive them through ECS `secrets`.
#
# DATABASE_URL: stored as one full-URL secret assembled here from the RDS endpoint, role
# rebox_app and the generated APP_DB_PASSWORD (alphanumeric, so no URL encoding). The apps read
# DATABASE_URL directly; no entrypoint wrapper. The migrate task sets the same password on the
# role, so both always come from this one random_password.
resource "random_password" "token" {
  for_each = toset(["INGEST_TOKEN", "ADMIN_TOKEN", "APP_DB_PASSWORD"])
  length   = 48
  special  = false
}

resource "aws_secretsmanager_secret" "generated" {
  for_each                = random_password.token
  name                    = "${local.name}/${each.key}"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "generated" {
  for_each      = random_password.token
  secret_id     = aws_secretsmanager_secret.generated[each.key].id
  secret_string = each.value.result
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${local.name}/DATABASE_URL"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgres://rebox_app:${random_password.token["APP_DB_PASSWORD"].result}@${local.db_host}:5432/rebox?sslmode=require"
}

# Optional tokens: created empty; set the value out of band, then add the name to
# var.optional_secrets so the worker receives it (an ECS secret without a value fails task start).
resource "aws_secretsmanager_secret" "optional" {
  for_each                = toset(["GITHUB_TOKEN", "X_BEARER_TOKEN"])
  name                    = "${local.name}/${each.key}"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 7
}

locals {
  secret_arn = merge(
    { for k, s in aws_secretsmanager_secret.generated : k => s.arn },
    { for k, s in aws_secretsmanager_secret.optional : k => s.arn },
    { DATABASE_URL = aws_secretsmanager_secret.database_url.arn },
  )
}
