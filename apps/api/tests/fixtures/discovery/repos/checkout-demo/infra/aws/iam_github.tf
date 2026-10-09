# GitHub Actions deploys with short-lived OIDC credentials. No access keys exist.

locals {
  github_oidc_url          = "token.actions.githubusercontent.com"
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::${local.account_id}:oidc-provider/${local.github_oidc_url}"

  cluster_arn  = aws_ecs_cluster.main.arn
  service_arns = concat([aws_ecs_service.worker.id], aws_ecs_service.api[*].id)
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://${local.github_oidc_url}"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Exact match: only workflows on this branch of this repo. Pull requests, other
    # branches, tags, and GitHub environments all produce a different `sub`. Both the
    # plain and the immutable (owner and repo IDs) forms of the repo name are accepted.
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_url}:sub"
      values = [
        for repo in compact([var.github_repository, var.github_repository_immutable]) :
        "repo:${repo}:ref:refs/heads/${var.github_branch}"
      ]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name                 = "${var.name}-github-deploy"
  description          = "Assumed by GitHub Actions (${var.github_repository}@${var.github_branch}) to deploy checkout"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # no resource-level permissions for this action
  }

  statement {
    sid = "PushImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [for repo in aws_ecr_repository.app : repo.arn]
  }

  statement {
    sid = "TaskDefinitions"
    # Neither action supports resource-level permissions.
    actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid       = "TagTaskDefinitionsOnRegister"
    actions   = ["ecs:TagResource"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "ecs:CreateAction"
      values   = ["RegisterTaskDefinition"]
    }
  }

  statement {
    sid       = "DeployServices"
    actions   = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = local.service_arns
  }

  statement {
    sid       = "RunMigrationTask"
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${aws_ecs_task_definition.api.family}:*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [local.cluster_arn]
    }
  }

  statement {
    sid       = "WatchMigrationTask"
    actions   = ["ecs:DescribeTasks"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task/${aws_ecs_cluster.main.name}/*"]
  }

  statement {
    sid       = "FindApiTasks"
    actions   = ["ecs:ListTasks"]
    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [local.cluster_arn]
    }
  }

  statement {
    sid       = "ReadApiPublicIp"
    actions   = ["ec2:DescribeNetworkInterfaces"]
    resources = ["*"] # no resource-level permissions for this action
  }

  statement {
    sid       = "ReadMigrationLogs"
    actions   = ["logs:GetLogEvents"]
    resources = ["${aws_cloudwatch_log_group.app["api"].arn}:log-stream:*"]
  }

  statement {
    sid       = "PassTaskRoles"
    actions   = ["iam:PassRole"]
    resources = [for role in aws_iam_role.task_execution : role.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}
