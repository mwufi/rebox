# RDS PostgreSQL 16. The master password is generated and stored by RDS in Secrets Manager
# (manage_master_user_password); only the migrate task reads it.
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier                    = local.name
  engine                        = "postgres"
  engine_version                = "16"
  instance_class                = var.db_instance_class
  allocated_storage             = var.db_allocated_storage
  max_allocated_storage         = var.db_allocated_storage * 5
  storage_type                  = "gp3"
  storage_encrypted             = true
  kms_key_id                    = aws_kms_key.main.arn
  db_name                       = "rebox"
  username                      = "rebox_owner"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn
  db_subnet_group_name          = aws_db_subnet_group.main.name
  vpc_security_group_ids        = [aws_security_group.db.id]
  publicly_accessible           = false
  multi_az                      = false
  backup_retention_period       = 7
  copy_tags_to_snapshot         = true
  auto_minor_version_upgrade    = true
  deletion_protection           = var.db_deletion_protection
  skip_final_snapshot           = false
  final_snapshot_identifier     = "${local.name}-final"
  performance_insights_enabled  = false
}

locals {
  db_host          = aws_db_instance.main.address
  db_master_secret = aws_db_instance.main.master_user_secret[0].secret_arn
  # No password here: the migrate task gets it as PGPASSWORD from the RDS-managed secret.
  migrate_database_url = "postgres://${aws_db_instance.main.username}@${local.db_host}:5432/rebox?sslmode=require"
}
