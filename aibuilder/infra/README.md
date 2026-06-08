# aibuilder hosting infra

OpenTofu stack that provisions the aibuilder hosting environment in
the `govtech-sandbox` AWS account (us-east-1).

## First deploy

1. `aws sso login --sso-session govtech`
2. `make init`
3. `make plan` — review
4. `make apply`
5. Enable Claude Opus 4.6 in the Bedrock console of govtech-sandbox
   (Bedrock → Model access → Manage model access → Anthropic → Claude Opus 4.6 → Request access).
6. Take the output `cloudfront_domain` and visit `https://<that>/`.
7. The chat UI will prompt you for the bearer token on first send.
   Get it: `aws ssm get-parameter --name /aibuilder/auth-token --with-decryption --query Parameter.Value --output text --profile govtech-sandbox`.

## Subsequent deploys

GitHub Actions handles them on push to `main` (see
`.github/workflows/aibuilder-deploy.yml`). Manual `make apply` is
only needed for infrastructure changes — image updates roll
automatically.

## State backend

Local state (gitignored). Single-developer / GitHub-Actions-only
applies — multi-developer applies would conflict. Migrating to an S3
backend is a follow-up (see commented block in `providers.tf`).
