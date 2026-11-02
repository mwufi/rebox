resource "aws_ecs_cluster" "main" {
  name = local.name
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(["api", "worker", "migrate", "scheduler"])
  name              = "/rebox/${var.env}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

locals {
  common_env = [
    { name = "REBOX_ENV", value = "prod" },
  ]
  worker_env = concat(local.common_env, [
    # Private DNS (Cloud Map) straight to the api tasks, not through the public ALB.
    { name = "API_URL", value = "http://api.${aws_service_discovery_private_dns_namespace.main.name}:3000" },
    { name = "RAW_STORE", value = "s3://${aws_s3_bucket.data.bucket}/raw" },
    { name = "AWS_REGION", value = var.region },
    { name = "USER_AGENT", value = var.user_agent },
  ])
  worker_secrets = concat(
    [for k in ["DATABASE_URL", "INGEST_TOKEN"] : { name = k, valueFrom = local.secret_arn[k] }],
    [for k in var.optional_secrets : { name = k, valueFrom = local.secret_arn[k] }],
  )

  log_config = { for k, g in aws_cloudwatch_log_group.this : k => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = g.name
      awslogs-region        = var.region
      awslogs-stream-prefix = k
    }
  } }

  task_defs = {
    api = {
      cpu     = 512, memory = 1024, image = local.api_image, role = "api"
      command = null, entryPoint = null
      env     = concat(local.common_env, [{ name = "PORT", value = "3000" }])
      secrets = [for k in ["DATABASE_URL", "INGEST_TOKEN", "ADMIN_TOKEN"] : { name = k, valueFrom = local.secret_arn[k] }]
      ports   = [{ containerPort = 3000, protocol = "tcp" }]
    }
    worker = {
      cpu     = 512, memory = 1024, image = local.worker_image, role = "worker"
      command = ["run"], entryPoint = null
      env     = local.worker_env, secrets = local.worker_secrets, ports = []
    }
    scheduler = {
      cpu     = 256, memory = 512, image = local.worker_image, role = "worker"
      command = ["schedule-due"], entryPoint = null
      env     = local.worker_env, secrets = local.worker_secrets, ports = []
    }
    # Same api image. Owner password comes from the RDS-managed secret (JSON key "password").
    migrate = {
      cpu     = 256, memory = 512, image = local.api_image, role = "migrate"
      command = null, entryPoint = ["/app/infra/docker/migrate.sh"]
      env = [
        { name = "MIGRATE_DATABASE_URL", value = local.migrate_database_url },
        { name = "PGSSLMODE", value = "require" },
      ]
      secrets = [
        { name = "PGPASSWORD", valueFrom = "${local.db_master_secret}:password::" },
        { name = "APP_DB_PASSWORD", valueFrom = local.secret_arn.APP_DB_PASSWORD },
      ]
      ports = []
    }
  }
}

resource "aws_ecs_task_definition" "this" {
  for_each                 = local.task_defs
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.exec[each.key == "scheduler" ? "worker" : each.key].arn
  task_role_arn            = aws_iam_role.task[each.value.role].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([merge(
    {
      name                   = each.key
      image                  = each.value.image
      essential              = true
      environment            = each.value.env
      secrets                = each.value.secrets
      portMappings           = each.value.ports
      readonlyRootFilesystem = false
      logConfiguration       = local.log_config[each.key]
    },
    each.value.command == null ? {} : { command = each.value.command },
    each.value.entryPoint == null ? {} : { entryPoint = each.value.entryPoint },
  )])
}

resource "aws_ecs_service" "api" {
  name                   = "api"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.this["api"].arn
  desired_count          = var.api_desired_count
  launch_type            = "FARGATE"
  enable_execute_command = false
  network_configuration {
    subnets          = local.task_subnet_ids
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = local.assign_public_ip
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 3000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  service_registries {
    registry_arn = aws_service_discovery_service.api.arn
  }
  depends_on = [aws_lb_listener.http]
}

# api.<env>.rebox.internal → api task IPs, for the worker's API_URL.
resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${var.env}.rebox.internal"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "api" {
  name = "api"
  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }
  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.this["worker"].arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = local.task_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = local.assign_public_ip
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
