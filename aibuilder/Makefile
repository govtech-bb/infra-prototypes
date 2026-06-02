.PHONY: check lint format test install install-dev

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

check: lint test
