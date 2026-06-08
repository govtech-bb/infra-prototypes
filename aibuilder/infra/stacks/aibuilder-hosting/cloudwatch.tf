resource "aws_cloudwatch_log_group" "task" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7

  tags = { Name = "${local.name}-task-logs" }
}
