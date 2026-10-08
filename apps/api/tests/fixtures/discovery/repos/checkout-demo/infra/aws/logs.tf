resource "aws_cloudwatch_log_group" "app" {
  for_each = local.services

  name              = "/ecs/${var.name}-${each.key}"
  retention_in_days = 7
}
