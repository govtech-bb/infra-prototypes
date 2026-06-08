locals {
  name = "${var.project}-${var.env}"

  common_tags = {
    Project   = var.project
    Env       = var.env
    ManagedBy = "OpenTofu"
    Stack     = "aibuilder-hosting"
  }
}
