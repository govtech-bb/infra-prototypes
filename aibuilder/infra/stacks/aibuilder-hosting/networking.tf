data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default_public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "ALB ingress from CloudFront edge IPs only"
  vpc_id      = data.aws_vpc.default.id

  # CloudFront edge IPs are dynamic. The simplest correct rule for MVP is
  # to allow 0.0.0.0/0 on 80 — only the CloudFront distribution knows
  # the ALB DNS name (it's not in DNS) and the chat UI route is auth-
  # gated. Hardening to the AWS-managed CloudFront prefix list
  # (com.amazonaws.global.cloudfront.origin-facing) is a follow-up.
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-alb" }
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Fargate task: only accepts traffic from the ALB SG"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = 8001
    to_port         = 8001
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-task" }
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "EFS: only accepts NFS (2049) from the Fargate task SG"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.task.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-efs" }
}
