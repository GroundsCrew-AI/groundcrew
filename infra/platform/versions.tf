terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # The existing state bucket in 881050425883 (encrypted, versioned), its own key. Native S3
  # locking (use_lockfile). No secret ever goes into this state: only the SSH public key and the
  # admin address (decision 6.4, docs/phase6-plan.md section 4).
  backend "s3" {
    bucket       = "groundcrew-checkout-tfstate-881050425883"
    key          = "groundcrew/platform.tfstate"
    region       = "us-east-2"
    profile      = "groundcrew-demo"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region  = "us-east-2"
  profile = "groundcrew-demo"

  # Refuse to touch any account but Groundcrew's, even if the profile is misconfigured.
  allowed_account_ids = ["881050425883"]

  default_tags {
    tags = {
      project           = "groundcrew"
      "groundcrew:role" = "platform"
    }
  }
}
