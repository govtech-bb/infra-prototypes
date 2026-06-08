# Outputs are added by individual resource files as they land.
# After everything is up, expect:
#   - cloudfront_domain   (the d<rand>.cloudfront.net URL to visit)
#   - ecr_repository_url  (for `docker push` from CI)
#   - ecs_cluster_name    (for CI to target with update-service)
#   - ecs_service_name
#   - github_deploy_role_arn
