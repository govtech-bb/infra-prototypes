resource "aws_ecr_repository" "aibuilder" {
  name                 = local.name
  image_tag_mutability = "MUTABLE" # `latest` tag gets reused on each deploy

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.common_tags
}

resource "aws_ecr_lifecycle_policy" "aibuilder" {
  repository = aws_ecr_repository.aibuilder.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 sha-tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 10
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Delete untagged after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      }
    ]
  })
}

output "ecr_repository_url" {
  value       = aws_ecr_repository.aibuilder.repository_url
  description = "ECR URL to push images to (used by GitHub Actions)"
}
