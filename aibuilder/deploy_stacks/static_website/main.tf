terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

locals {
  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.env
      ManagedBy   = "opentofu"
    },
    var.site_title != "" ? { Title = var.site_title } : {},
    var.owner_name != "" ? { OwnerName = var.owner_name } : {},
    var.owner_email != "" ? { OwnerEmail = var.owner_email } : {},
  )
}

# ── S3 Bucket ──────────────────────────────────────────────────────────────────
module "s3" {
  source = "./modules/s3-static-site"

  project_name      = var.project_name
  env               = var.env
  enable_versioning = var.enable_versioning
}

# ── CloudFront Distribution ────────────────────────────────────────────────────
module "cloudfront" {
  source = "./modules/cloudfront"

  project_name                   = var.project_name
  env                            = var.env
  s3_bucket_regional_domain_name = module.s3.bucket_regional_domain_name
  index_document                 = var.index_document
  is_spa                         = var.is_spa
  price_class                    = var.price_class
  acm_certificate_arn            = var.acm_certificate_arn
  domain_names                   = var.domain_names
}

# ── Bucket Policy ──────────────────────────────────────────────────────────────
# Lives in the stack (not a module) to break the circular dependency between
# S3 (needs CF ARN for policy) and CloudFront (needs S3 domain for origin).
resource "aws_s3_bucket_policy" "cloudfront_access" {
  bucket = module.s3.bucket_id
  policy = data.aws_iam_policy_document.cloudfront_access.json

  depends_on = [module.s3, module.cloudfront]
}

data "aws_iam_policy_document" "cloudfront_access" {
  statement {
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions   = ["s3:GetObject"]
    resources = ["${module.s3.bucket_arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [module.cloudfront.distribution_arn]
    }
  }
}
