# Local state is fine for prototypes — just don't lose the .tfstate file.
#
# When you're ready to graduate to a shared/remote state, uncomment this block
# and run `tofu init` to migrate state to S3.
#
# terraform {
#   backend "s3" {
#     bucket         = "your-tfstate-bucket"
#     key            = "${var.project_name}/${var.env}/terraform.tfstate"
#     region         = "us-east-1"
#     dynamodb_table = "terraform-state-locks"
#     encrypt        = true
#   }
# }
