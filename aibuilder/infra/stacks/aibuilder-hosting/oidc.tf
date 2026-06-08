# Conditionally create the GitHub OIDC IdP. If the IdP already exists in
# the account (e.g. another stack set it up), pass its ARN via
# var.github_oidc_provider_arn and creation is skipped. Leaving the
# variable at its default empty string causes the provider to be created
# here. This makes the stack safe to apply in any account.

variable "github_oidc_provider_arn" {
  description = "ARN of a pre-existing GitHub OIDC IdP. Leave empty to create one in this stack."
  type        = string
  default     = ""
}

locals {
  github_oidc_url = "https://token.actions.githubusercontent.com"

  # Resolve to the pre-existing ARN when supplied, otherwise the one we create.
  github_oidc_provider_arn = var.github_oidc_provider_arn != "" ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.github_oidc_provider_arn != "" ? 0 : 1

  url             = local.github_oidc_url
  client_id_list  = ["sts.amazonaws.com"]
  # GitHub's OIDC thumbprint — AWS docs publish this list; current as of 2024.
  # AWS ignores the thumbprint for token.actions.githubusercontent.com (it is
  # in their allowlist), so this value is cosmetic but must be present.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_deploy" {
  name = "${local.name}-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          # Trust only main branch + PRs in the configured repo.
          "token.actions.githubusercontent.com:sub" = [
            "repo:${var.github_repo}:ref:refs/heads/main",
            "repo:${var.github_repo}:pull_request",
          ]
        }
      }
    }]
  })
}

# Deploy role gets exactly what GitHub Actions needs: ECR push +
# describe + ECS update-service + describe-services.
resource "aws_iam_role_policy" "github_deploy" {
  name = "${local.name}-github-deploy"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeRepositories",
          "ecr:DescribeImages",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = aws_ecr_repository.aibuilder.arn
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:DescribeServices",
          "ecs:UpdateService",
        ]
        Resource = aws_ecs_service.aibuilder.id
      },
      {
        Effect = "Allow"
        Action = ["ecs:DescribeTaskDefinition"]
        Resource = "*"
      },
    ]
  })
}

output "github_deploy_role_arn" {
  value       = aws_iam_role.github_deploy.arn
  description = "Role ARN GitHub Actions assumes via OIDC; goes in .github/workflows/aibuilder-deploy.yml"
}
