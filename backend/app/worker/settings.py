"""arq worker settings. Real tasks added in M1."""

from arq.connections import RedisSettings

from app.core.config import settings


async def ping(ctx: dict) -> str:
    """Placeholder task. arq requires at least one registered function to boot;
    this keeps the worker a valid stub until real handlers arrive in M1."""
    return "pong"


class WorkerSettings:
    """Entry point for the arq worker.

    `arq app.worker.settings.WorkerSettings` boots a worker that connects to
    Redis and waits for jobs. Only the `ping` stub is registered for now.
    """

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [ping]
