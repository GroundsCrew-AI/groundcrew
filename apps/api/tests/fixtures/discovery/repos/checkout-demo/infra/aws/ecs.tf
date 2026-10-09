resource "aws_ecs_cluster" "main" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "disabled" # extra CloudWatch cost; basic ECS metrics are enough for the demo
  }
}

locals {
  # Placeholder until the first CI deploy pushes a SHA-tagged image. Services start at
  # desired_count 0, so nothing tries to pull it.
  bootstrap_tag = "bootstrap"

  database_url_secret = [{
    name      = "DATABASE_URL"
    valueFrom = aws_secretsmanager_secret.database_url.arn
  }]

  log_config = {
    for svc in local.services : svc => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app[svc].name
        awslogs-region        = var.region
        awslogs-stream-prefix = svc
      }
    }
  }
}

# The api task definition always exists, even with api_platform = "azure": CI runs
# migrations and `make seed` runs the seed as one-off tasks from it.
# CI registers new revisions (image tag = commit SHA) from the latest one, so cpu, memory,
# and container settings here are the baseline, overridden by api/ecs-task.json.
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.task_execution["api"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name         = "api"
    image        = "${aws_ecr_repository.app["api"].repository_url}:${local.bootstrap_tag}"
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = [{ name = "LOG_LEVEL", value = "INFO" }]
    secrets      = local.database_url_secret
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }
    logConfiguration = local.log_config["api"]
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.task_execution["worker"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = "${aws_ecr_repository.app["worker"].repository_url}:${local.bootstrap_tag}"
    essential = true
    environment = [
      { name = "LOG_LEVEL", value = "INFO" },
      { name = "POLL_INTERVAL_SECONDS", value = "10" },
      { name = "BATCH_SIZE", value = "100" },
    ]
    secrets          = local.database_url_secret
    logConfiguration = local.log_config["worker"]
  }])
}

resource "aws_ecs_service" "api" {
  count = local.api_on_aws ? 1 : 0

  name             = "${var.name}-api"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.api.arn
  desired_count    = var.api_desired_count
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id, aws_security_group.api[0].id]
    assign_public_ip = true
  }

  # Mark a deploy failed after repeated task failures, but do not roll back by itself:
  # the failure scenarios need the broken state to stay visible.
  deployment_circuit_breaker {
    enable   = true
    rollback = false
  }

  lifecycle {
    # CI owns the running task definition and the count after the first deploy.
    ignore_changes = [task_definition, desired_count]
  }
}

resource "aws_ecs_service" "worker" {
  name             = "${var.name}-worker"
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.worker.arn
  desired_count    = var.worker_desired_count
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true # needed to pull from ECR without a NAT Gateway
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = false
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
