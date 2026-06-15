output "bucket_name" {
  description = "S3 bucket name — upload your build files here"
  value       = module.s3.bucket_id
}

output "site_url" {
  description = "Your site URL"
  value       = "https://${module.cloudfront.distribution_domain_name}"
}

output "cloudfront_distribution_id" {
  description = "Use this to invalidate the cache after deploying new files"
  value       = module.cloudfront.distribution_id
}

output "upload_command" {
  description = "Quick command to sync your build to S3"
  value       = "aws s3 sync ./dist s3://${module.s3.bucket_id} --delete"
}

output "invalidate_command" {
  description = "Quick command to bust the CloudFront cache"
  value       = "aws cloudfront create-invalidation --distribution-id ${module.cloudfront.distribution_id} --paths '/*'"
}
