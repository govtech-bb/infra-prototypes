variable "project_name" {
  type = string
}

variable "env" {
  type = string
}

variable "s3_bucket_regional_domain_name" {
  description = "Regional domain name of the S3 bucket to use as origin"
  type        = string
}

variable "index_document" {
  description = "Default root object served by CloudFront"
  type        = string
  default     = "index.html"
}

variable "is_spa" {
  description = "If true, routes 403/404 errors back to index.html for SPA client-side routing"
  type        = bool
  default     = false
}

variable "price_class" {
  description = "CloudFront price class. PriceClass_100 = US/EU only (cheapest for prototypes)"
  type        = string
  default     = "PriceClass_100"
}

variable "default_ttl" {
  description = "Default TTL for cached objects in seconds"
  type        = number
  default     = 3600
}

variable "max_ttl" {
  description = "Maximum TTL for cached objects in seconds"
  type        = number
  default     = 86400
}

# ── Graduate to these when moving toward production ────────────────────────────

variable "acm_certificate_arn" {
  description = "ACM certificate ARN for custom domain (must be in us-east-1). Leave null for prototype."
  type        = string
  default     = null
}

variable "domain_names" {
  description = "Custom domain names (CNAMEs). Leave empty for prototype — uses *.cloudfront.net."
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
