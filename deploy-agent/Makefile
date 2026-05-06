.PHONY: check lint format test infra-validate destroy-list destroy-all install install-dev

install:
	pip3 install -r requirements.txt

install-dev: install
	pip3 install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

test:
	pytest tests/ -v

infra-validate:
	cd ../infra/stacks/static-website && \
	  tofu fmt -recursive -check && \
	  tofu init -backend=false -upgrade && \
	  tofu validate

check: lint test infra-validate

destroy-list:
	python3 ../scripts/list_deployments.py

destroy-all:
	python3 ../scripts/destroy_all.py
