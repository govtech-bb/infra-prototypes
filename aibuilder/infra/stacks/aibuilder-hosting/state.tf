# aibuilder/infra/stacks/aibuilder-hosting/state.tf
# Tofu state for deployed prototypes lives here. The hosting stack
# itself still uses local state — this is for the deploy engine's
# per-deployment state files (key = deployments/<project>-<env>.tfstate).

resource "aws_s3_bucket" "deploy_state" {
  bucket = "aibuilder-deploy-state-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "aibuilder-deploy-state" }
}

resource "aws_s3_bucket_versioning" "deploy_state" {
  bucket = aws_s3_bucket.deploy_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy_state" {
  bucket = aws_s3_bucket.deploy_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "deploy_state" {
  bucket                  = aws_s3_bucket.deploy_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "deploy_lock" {
  name         = "aibuilder-deploy-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = { Name = "aibuilder-deploy-lock" }
}

output "deploy_state_bucket" {
  value = aws_s3_bucket.deploy_state.id
}

output "deploy_lock_table" {
  value = aws_dynamodb_table.deploy_lock.name
}
