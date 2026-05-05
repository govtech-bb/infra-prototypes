# ── Config ─────────────────────────────────────────────────────────────────────
# Override any of these on the command line, e.g.:
#   make deploy PROJECT=myapp ENV=chris-test
#   make deploy PROJECT=landing ENV=staging STACK=static-website
STACK   ?= static-website
PROJECT ?= myapp
ENV     ?= dev
DIR      = stacks/$(STACK)

# ── Commands ───────────────────────────────────────────────────────────────────
.PHONY: init plan deploy destroy upload

init:
	cd $(DIR) && tofu init

plan:
	cd $(DIR) && tofu plan \
	  -var="project_name=$(PROJECT)" \
	  -var="env=$(ENV)"

deploy:
	cd $(DIR) && tofu apply -auto-approve \
	  -var="project_name=$(PROJECT)" \
	  -var="env=$(ENV)"
	@echo ""
	@echo "✓ Deployed. Your site URL:"
	@cd $(DIR) && tofu output site_url

destroy:
	cd $(DIR) && tofu destroy -auto-approve \
	  -var="project_name=$(PROJECT)" \
	  -var="env=$(ENV)"

# Upload build files and bust CloudFront cache in one shot
# Usage: make upload PROJECT=myapp ENV=chris-test
upload:
	$(eval BUCKET := $(shell cd $(DIR) && tofu output -raw bucket_name))
	$(eval DIST_ID := $(shell cd $(DIR) && tofu output -raw cloudfront_distribution_id))
	@echo "Syncing ./dist to s3://$(BUCKET)..."
	aws s3 sync ./dist s3://$(BUCKET) --delete
	@echo "Invalidating CloudFront cache..."
	aws cloudfront create-invalidation --distribution-id $(DIST_ID) --paths '/*'
	@echo ""
	@echo "✓ Done. Your site URL:"
	@cd $(DIR) && tofu output site_url
