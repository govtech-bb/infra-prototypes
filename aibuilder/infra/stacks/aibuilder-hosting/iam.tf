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
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${aws_ssm_parameter.auth_token.name}"
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
      # us-east-1 + us-west-2 are common Bedrock regions. Scope here
      # to us-east-1 specifically since that's our region.
      Resource = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id}",
      ]
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
