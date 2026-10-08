data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  api_on_aws = var.api_platform == "aws"

  # Fixed AZ names instead of a data source keep `plan` free of EC2 read calls.
  azs = ["${var.region}a", "${var.region}b"]

  services = toset(["api", "worker"])
}
