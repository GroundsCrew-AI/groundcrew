# The platform identity: replaces the groundcrew-platform IAM user and its access keys. Its only
# permission is assuming roles named groundcrew-* (customers' connect roles, which also require
# that workspace's ExternalId). Customers trust this role's ARN (GroundcrewPrincipalArn).

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    sid     = "Ec2Only"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "assume_customer_roles" {
  statement {
    sid       = "AssumeCustomerConnectRoles"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::*:role/groundcrew-*"]
  }
}

resource "aws_iam_role" "platform" {
  name                 = "groundcrew-platform"
  description          = "Groundcrew platform host: assumes customers' groundcrew-* roles, nothing else"
  assume_role_policy   = data.aws_iam_policy_document.ec2_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "assume_customer_roles" {
  name   = "groundcrew-assume-customer-roles"
  role   = aws_iam_role.platform.id
  policy = data.aws_iam_policy_document.assume_customer_roles.json
}

resource "aws_iam_instance_profile" "platform" {
  name = "groundcrew-platform"
  role = aws_iam_role.platform.name
}
