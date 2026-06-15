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
