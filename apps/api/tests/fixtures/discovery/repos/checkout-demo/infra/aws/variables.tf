variable "region" {
  description = "AWS region for every resource in this stack."
  type        = string
  default     = "us-east-2"
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "checkout"
}

variable "api_platform" {
  description = "Where the api runs: \"aws\" (ECS Fargate service here) or \"azure\" (infra/azure). With \"azure\" the api service is removed and RDS becomes publicly reachable, limited to allowed_db_cidrs."
  type        = string
  default     = "aws"

  validation {
    condition     = contains(["aws", "azure"], var.api_platform)
    error_message = "api_platform must be \"aws\" or \"azure\"."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. Two /24 public subnets are carved from it."
  type        = string
  default     = "10.20.0.0/16"
}

variable "api_desired_count" {
  description = "Initial api task count. Starts at 0 because no image exists before the first CI deploy; CI sets it to 1. Terraform ignores later changes."
  type        = number
  default     = 0
}

variable "worker_desired_count" {
  description = "Initial worker task count. Same rules as api_desired_count."
  type        = number
  default     = 0
}

variable "allowed_db_cidrs" {
  description = "Extra CIDRs allowed to reach Postgres on 5432, for the Azure api's outbound IPs (as /32). Never 0.0.0.0/0."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.allowed_db_cidrs, "0.0.0.0/0")
    error_message = "0.0.0.0/0 is not allowed on the database."
  }
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "github_repository" {
  description = "GitHub repo (owner/name) allowed to assume the deploy role."
  type        = string
  default     = "GroundsCrew-AI/checkout-demo"
}

variable "github_repository_immutable" {
  description = "The same repo as owner@owner_id/name@repo_id. GitHub uses this form in the OIDC `sub` when the repo has immutable subjects turned on (gh api repos/OWNER/REPO/actions/oidc/customization/sub). Empty to accept only the plain form."
  type        = string
  default     = "GroundsCrew-AI@338275343/checkout-demo@1408958338"
}

variable "github_branch" {
  description = "Only workflows running on this branch can assume the deploy role."
  type        = string
  default     = "main"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub Actions OIDC provider. An account can have only one; set false if it already exists and it will be referenced by ARN."
  type        = bool
  default     = true
}
