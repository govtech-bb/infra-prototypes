# We don't generate the token in OpenTofu — that would put it in the
# state file. After first apply, manually set:
#   aws ssm put-parameter \
#     --name /aibuilder/auth-token \
#     --type SecureString \
#     --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
#     --overwrite \
#     --profile govtech-sandbox
# The lifecycle ignore_changes block means OpenTofu won't try to
# overwrite a value set out-of-band.
resource "aws_ssm_parameter" "auth_token" {
  name        = "/aibuilder/auth-token"
  description = "Bearer token required for /api/* requests; injected as AIBUILDER_TOKEN"
  type        = "SecureString"
  value       = "PLACEHOLDER_OVERWRITE_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${local.name}-auth-token" }
}

resource "aws_ssm_parameter" "github_token" {
  name        = "/aibuilder/github-token"
  description = "Fine-grained GitHub PAT (read-only, govtech-bb org) for cloning private repos"
  type        = "SecureString"
  value       = "PLACEHOLDER_OVERWRITE_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${local.name}-github-token" }
}

resource "aws_ssm_parameter" "github_app_id" {
  name        = "/aibuilder/github-app-id"
  description = "Numeric App ID for the aibuilder GitHub App (not secret)"
  type        = "String"
  value       = "PLACEHOLDER_OVERWRITE_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${local.name}-github-app-id" }
}

resource "aws_ssm_parameter" "github_app_installation_id" {
  name        = "/aibuilder/github-app-installation-id"
  description = "Numeric Installation ID of the aibuilder App on govtech-bb (not secret)"
  type        = "String"
  value       = "PLACEHOLDER_OVERWRITE_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${local.name}-github-app-installation-id" }
}

resource "aws_ssm_parameter" "github_app_private_key" {
  name        = "/aibuilder/github-app-private-key"
  description = "PEM-encoded RSA private key for the aibuilder GitHub App"
  type        = "SecureString"
  value       = "PLACEHOLDER_OVERWRITE_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = { Name = "${local.name}-github-app-private-key" }
}
