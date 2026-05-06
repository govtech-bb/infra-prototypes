resource "aws_s3_bucket" "this" {
  bucket        = "${var.project_name}-${var.env}-static"
  force_destroy = true

  tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.env
  })
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  count  = var.enable_versioning ? 1 : 0
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}
