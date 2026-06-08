.PHONY: init plan apply destroy fmt validate

STACK := aibuilder-hosting
STACK_DIR := stacks/$(STACK)
AWS_PROFILE ?= govtech-sandbox

init:
	cd $(STACK_DIR) && tofu init -upgrade

plan:
	cd $(STACK_DIR) && AWS_PROFILE=$(AWS_PROFILE) tofu plan

apply:
	cd $(STACK_DIR) && AWS_PROFILE=$(AWS_PROFILE) tofu apply

destroy:
	cd $(STACK_DIR) && AWS_PROFILE=$(AWS_PROFILE) tofu destroy

fmt:
	tofu fmt -recursive

validate:
	cd $(STACK_DIR) && tofu init -backend=false -upgrade && tofu validate
