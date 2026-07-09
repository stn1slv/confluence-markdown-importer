.PHONY: setup test lint format build run clean upgrade-deps

setup:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

build:
	uv build

run:
	uv run cmi --help

clean:
	rm -rf dist .pytest_cache .mypy_cache .ruff_cache .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +

upgrade-deps:
	uv sync --upgrade
