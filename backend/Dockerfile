# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv: fast dependency resolver/installer. Copied from the official image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency manifests first so `uv sync` is cached and only re-runs
# when dependencies change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy the application source.
COPY . .

EXPOSE 8000

# Default command runs the API. docker-compose overrides this for the worker.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
