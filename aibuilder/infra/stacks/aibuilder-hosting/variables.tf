variable "aws_region" {
  description = "AWS region for the hosting stack."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Used as a prefix for AWS resource names + tags."
  type        = string
  default     = "aibuilder"
}

variable "env" {
  description = "Environment name (sandbox / staging / prod). Drives resource naming."
  type        = string
  default     = "sandbox"
}

variable "image_tag" {
  description = "ECR image tag the ECS task definition pins to. CI usually rotates this via `aws ecs update-service --force-new-deployment` after pushing a new image with this tag."
  type        = string
  default     = "latest"
}

variable "bedrock_model_id" {
  description = "Bedrock model ID for the AIBUILDER_BEDROCK_MODEL env var injected into the task."
  type        = string
  default     = "us.anthropic.claude-opus-4-6-v1"
}

variable "github_repo" {
  description = "GitHub repository (owner/name) allowed to assume the deploy role via OIDC."
  type        = string
  default     = "christophercorbin/infra-prototypes"
}
