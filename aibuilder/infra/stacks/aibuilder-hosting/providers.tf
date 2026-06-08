terraform {
  required_version = ">= 1.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # When ready to share state across developers / CI, uncomment + create
  # the bucket and DynamoDB table first, then `tofu init -migrate-state`.
  # backend "s3" {
  #   bucket         = "aibuilder-tofu-state-672203047922"
  #   key            = "aibuilder-hosting/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "aibuilder-tofu-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}
