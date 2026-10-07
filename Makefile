.PHONY: help build run stop lint test test-with-cov coverage security dependency-check check

help:
	@echo "Available commands:"
	@echo "  build         - Build the Docker image"
	@echo "  run           - Build and start the app in the foreground"
	@echo "  stop          - Stop the Docker Compose app"
	@echo "  lint          - Run linter"
	@echo "  test          - Run tests"
	@echo "  test-with-cov - Run tests with coverage"
	@echo "  coverage      - Run tests with coverage"
	@echo "  security      - Run security checks"
	@echo "  dependency-check - Check dependencies"
	@echo "  check         - Run all checks"

build:
	docker compose build

run:
	docker compose up --build

stop:
	docker compose down

lint:
	uv run ruff check .

test:
	uv run pytest tests/ -v -n auto

test-with-cov:
	uv run pytest tests/ -n auto --cov=golf_league --cov-branch --cov-report=term-missing --cov-fail-under=80 -v

coverage:
	uv run coverage run -m pytest tests/
	uv run coverage report
	uv run coverage html

security:
	uv pip check

dependency-check:
	uv pip list --outdated

# test-with-cov runs the whole suite, so `test` is not repeated here.
check: lint test-with-cov security dependency-check
