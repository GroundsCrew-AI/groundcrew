# Shared by every ECS task (api, worker, migration and seed one-off tasks). Outbound only.
resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "ECS tasks: outbound only. Postgres trusts this group."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.name}-tasks" }
}

resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.tasks.id
  description       = "ECR, Secrets Manager, CloudWatch Logs, Postgres"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# DEMO ONLY: the api is reached directly on its task public IP over plain HTTP, because
# there is no load balancer (cost rule). Vercel calls it server-side from changing IPs,
# so the port is open to the internet. The api serves fake demo data only.
resource "aws_security_group" "api" {
  count = local.api_on_aws ? 1 : 0

  name        = "${var.name}-api"
  description = "api task: port 8000 open to the internet (demo only, no load balancer)"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.name}-api" }
}

resource "aws_vpc_security_group_ingress_rule" "api_http" {
  count = local.api_on_aws ? 1 : 0

  security_group_id = aws_security_group.api[0].id
  description       = "api HTTP, demo only"
  ip_protocol       = "tcp"
  from_port         = 8000
  to_port           = 8000
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Postgres: 5432 from ECS tasks and allowed_db_cidrs only"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.name}-db" }
}

resource "aws_vpc_security_group_ingress_rule" "db_from_tasks" {
  security_group_id            = aws_security_group.db.id
  description                  = "Postgres from ECS tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.tasks.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_cidrs" {
  for_each = toset(var.allowed_db_cidrs)

  security_group_id = aws_security_group.db.id
  description       = "Postgres from an allowed CIDR (Azure api outbound IP)"
  ip_protocol       = "tcp"
  from_port         = 5432
  to_port           = 5432
  cidr_ipv4         = each.value
}
