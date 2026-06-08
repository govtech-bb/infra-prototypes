resource "aws_efs_file_system" "sessions" {
  creation_token   = "${local.name}-sessions"
  encrypted        = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = { Name = "${local.name}-sessions" }
}

resource "aws_efs_mount_target" "sessions" {
  for_each = toset(data.aws_subnets.default_public.ids)

  file_system_id  = aws_efs_file_system.sessions.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# Access point pins the container's view of the file system to a
# specific subdirectory + UID/GID. The Fargate task mounts the
# access point at /aibuilder/data inside the container.
resource "aws_efs_access_point" "sessions" {
  file_system_id = aws_efs_file_system.sessions.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/aibuilder-data"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "755"
    }
  }
}
