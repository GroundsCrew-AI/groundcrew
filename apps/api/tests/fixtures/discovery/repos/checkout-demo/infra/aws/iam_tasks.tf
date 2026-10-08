# One ECS task execution role per service. The apps make no AWS API calls, so there is no
# separate task role: these are the only roles the deploy role may pass.

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  for_each = local.services

  name               = "${var.name}-${each.key}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task_execution" {
  for_each = local.services

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action has no resource-level permissions
  }

  statement {
    sid = "PullOwnImage"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    # The api role also runs migrations and the seed, which use the api image.
    resources = [aws_ecr_repository.app[each.key].arn]
  }

  statement {
    sid       = "WriteOwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app[each.key].arn}:*"]
  }

  statement {
    sid       = "ReadDatabaseUrl"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.database_url.arn]
  }
}

resource "aws_iam_role_policy" "task_execution" {
  for_each = local.services

  name   = "task-execution"
  role   = aws_iam_role.task_execution[each.key].id
  policy = data.aws_iam_policy_document.task_execution[each.key].json
}
