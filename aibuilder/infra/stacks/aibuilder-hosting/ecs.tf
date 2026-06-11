resource "aws_ecs_cluster" "aibuilder" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # cost-saving; enable later if observability calls for it
  }
}

resource "aws_ecs_task_definition" "aibuilder" {
  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256" # 0.25 vCPU
  memory                   = "512" # 0.5 GB
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "aibuilder"
    image     = "${aws_ecr_repository.aibuilder.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{
      containerPort = 8001
      protocol      = "tcp"
    }]

    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "AIBUILDER_BEDROCK_MODEL", value = var.bedrock_model_id },
      { name = "AIBUILDER_DB", value = "/aibuilder/data/sessions.db" },
    ]

    secrets = [
      {
        name      = "AIBUILDER_TOKEN"
        valueFrom = aws_ssm_parameter.auth_token.arn
      }
    ]

    mountPoints = [{
      sourceVolume  = "sessions"
      containerPath = "/aibuilder/data"
      readOnly      = false
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "aibuilder"
      }
    }
  }])

  volume {
    name = "sessions"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.sessions.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.sessions.id
        iam             = "ENABLED"
      }
    }
  }
}

resource "aws_ecs_service" "aibuilder" {
  name            = local.name
  cluster         = aws_ecs_cluster.aibuilder.id
  task_definition = aws_ecs_task_definition.aibuilder.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default_public.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true # required for tasks in public subnets to pull from ECR
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.aibuilder.arn
    container_name   = "aibuilder"
    container_port   = 8001
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 200

  depends_on = [aws_lb_listener.http]
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.aibuilder.name
}

output "ecs_service_name" {
  value = aws_ecs_service.aibuilder.name
}
