# Task execution role: used by ECS itself (NOT the running container)
# to pull the image from ECR, write logs, and resolve secrets from SSM.
resource "aws_iam_role" "task_execution" {
  name = "${local.name}-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Lets the task execution role read the bearer-token SSM parameter
# at task launch (to inject as the AIBUILDER_TOKEN env var).
data "aws_caller_identity" "current" {}

resource "aws_iam_role_policy" "task_execution_ssm" {
  name = "${local.name}-task-execution-ssm"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["ssm:GetParameters"]
      Resource = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${aws_ssm_parameter.auth_token.name}",
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${aws_ssm_parameter.github_token.name}",
      ]
    }]
  })
}

# Task role: used by the running container's code (boto3 calls from
# inside the app). Gets Bedrock + Pricing API + EFS access.
resource "aws_iam_role" "task" {
  name = "${local.name}-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

locals {
  # Strip the `us.`/`global.`/`eu.`/`apac.` inference-profile prefix to get
  # the underlying foundation-model ID for IAM resource ARNs.
  bedrock_foundation_model_id = replace(var.bedrock_model_id, "/^(us|global|eu|apac)\\./", "")

  # When invoking a `us.` cross-region inference profile, Bedrock may route
  # the actual call to any of these regions — the task role needs
  # foundation-model permission in all of them, not just var.aws_region.
  bedrock_invoke_regions = ["us-east-1", "us-east-2", "us-west-2"]
}

resource "aws_iam_role_policy" "task_bedrock" {
  name = "${local.name}-task-bedrock"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      Resource = concat(
        [
          "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
        ],
        [
          for r in local.bedrock_invoke_regions :
          "arn:aws:bedrock:${r}::foundation-model/${local.bedrock_foundation_model_id}"
        ],
      )
    }]
  })
}

resource "aws_iam_role_policy" "task_pricing" {
  name = "${local.name}-task-pricing"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "pricing:GetProducts",
        "pricing:GetAttributeValues",
        "pricing:DescribeServices",
      ]
      # The Pricing API doesn't support resource-level ARN scoping.
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy" "task_efs" {
  name = "${local.name}-task-efs"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "elasticfilesystem:ClientMount",
        "elasticfilesystem:ClientWrite",
      ]
      Resource = [aws_efs_file_system.sessions.arn]
    }]
  })
}

resource "aws_iam_role_policy" "task_deploy_state" {
  name = "${local.name}-task-deploy-state"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.deploy_state.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.deploy_state.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.deploy_lock.arn
      },
    ]
  })
}
