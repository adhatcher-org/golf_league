.PHONY: help lint test coverage security dependency-check check

help:
	@echo "Available commands:"
	@echo "  lint          - Run linter"
	@echo "  test          - Run tests"
	@echo "  coverage      - Run tests with coverage"
	@echo "  security      - Run security checks"
	@echo "  dependency-check - Check dependencies"
	@echo "  check         - Run all checks"

lint:
	ruff check .

test:
	uv run pytest tests/ -v

coverage:
	uv run coverage run -m pytest tests/
	uv run coverage report
	uv run coverage html

security:
	uv pip check

dependency-check:
	uv pip list --outdated

check: lint test coverage security dependency-check
