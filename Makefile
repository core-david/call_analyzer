.PHONY: dev test lint

dev:           ## Start local environment
	docker compose up --build

test:          ## Run test suite
	cd backend && uv run pytest

lint:          ## Lint and format
	cd backend && uv run ruff check . && uv run ruff format .
