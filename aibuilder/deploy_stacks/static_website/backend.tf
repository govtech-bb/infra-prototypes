# Bucket, key, region, dynamodb_table are passed via -backend-config at init
# time so the same stack tree serves every deployment with a distinct state key.
terraform {
  backend "s3" {}
}
