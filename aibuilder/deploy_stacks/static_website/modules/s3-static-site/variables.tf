variable "project_name" {
  description = "Project name used for resource naming and tagging"
  type        = string
}

variable "env" {
  description = "Environment name (e.g., dev, staging, prod, or a prototype name)"
  type        = string
}

variable "enable_versioning" {
  description = "Enable S3 bucket versioning"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags to apply to resources"
  type        = map(string)
  default     = {}
}
