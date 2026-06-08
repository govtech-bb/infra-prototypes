resource "aws_lb" "aibuilder" {
  name               = local.name
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = data.aws_subnets.default_public.ids

  tags = { Name = local.name }
}

resource "aws_lb_target_group" "aibuilder" {
  name        = local.name
  port        = 8001
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id

  health_check {
    path                = "/api/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    matcher             = "200"
  }

  # Fargate replaces tasks during deploys; let the existing connections
  # finish quickly rather than holding the deploy.
  deregistration_delay = 30
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.aibuilder.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.aibuilder.arn
  }
}

output "alb_dns_name" {
  value       = aws_lb.aibuilder.dns_name
  description = "ALB internal-facing hostname; CloudFront uses this as its origin"
}
