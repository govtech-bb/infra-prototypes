variable "project_name" {
  description = "Short project or prototype name (e.g., 'myapp', 'landing-v2')"
  type        = string
}

variable "env" {
  description = "Environment or prototype identifier (e.g., 'dev', 'staging', 'chris-test')"
  type        = string
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

# ── Prototype defaults — these are all you need to get started ─────────────────

variable "index_document" {
  description = "Default root object served by CloudFront"
  type        = string
  default     = "index.html"
}

variable "is_spa" {
  description = "Enable SPA routing (redirects 403/404s to index.html for React/Vue/etc.)"
  type        = bool
  default     = false
}

variable "enable_versioning" {
  description = "Enable S3 bucket versioning"
  type        = bool
  default     = false
}

variable "price_class" {
  description = "CloudFront price class. PriceClass_100 = US/EU only (cheapest for prototypes)"
  type        = string
  default     = "PriceClass_100"
}

# ── Ownership + tagging (used by the deploy agent) ────────────────────────────

variable "site_title" {
  description = "Human-readable title of the website, used as an AWS tag"
  type        = string
  default     = ""
}

variable "owner_name" {
  description = "Full name of the site owner, used as an AWS tag"
  type        = string
  default     = ""
}

variable "owner_email" {
  description = "Email of the site owner, used as an AWS tag"
  type        = string
  default     = ""
}

# ── Graduate to these when moving toward production ────────────────────────────

variable "acm_certificate_arn" {
  description = "ACM cert ARN for custom domain (must be in us-east-1). Leave null for prototype."
  type        = string
  default     = null
}

variable "domain_names" {
  description = "Custom domain names. Leave empty for prototype — uses *.cloudfront.net."
  type        = list(string)
  default     = []
}
