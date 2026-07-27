"""arq worker settings."""

from arq.connections import RedisSettings

from app.core.config import settings
from app.worker.tasks import process_call


class WorkerSettings:
    """Entry point for the arq worker.

    `arq app.worker.settings.WorkerSettings` boots a worker that connects to
    Redis and waits for jobs.
    """

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [process_call]
    max_tries = 1        # no auto-retry in reduced M2; failures land in `failed`
    job_timeout = 900    # must exceed the 600s Deepgram request timeout + analysis
