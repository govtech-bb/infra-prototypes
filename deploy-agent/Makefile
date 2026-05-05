.PHONY: check lint format test infra-validate destroy-all install install-dev

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

destroy-all:
	python3 ../scripts/destroy_all.py
