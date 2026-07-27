from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Repo-root .env, resolved absolutely so config loads the same file regardless
# of working directory (scripts run from backend/, docker from repo root).
# config.py is backend/app/core/config.py -> parents[3] is the repo root.
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    # Deepgram — key comes from .env / environment, never a source default.
    deepgram_api_key: str = ""
    deepgram_request_timeout: int = 600   # seconds; classified provider_timeout beyond
    deepgram_max_concurrency: int = 5     # PAYG quota is 50; deliberate headroom (2.4)

    # LLM concurrency placeholder — used by M3, created alongside in 2.4
    llm_max_concurrency: int = 5


    # Google Gemini
    google_api_key: str = ""

    # Storage (R2 in prod, MinIO locally)
    storage_endpoint_url: str = "http://localhost:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"
    storage_bucket_name: str = "call-analyzer-audio"
    storage_region: str = "auto"          # R2 wants "auto"; MinIO ignores it

    # Postgres
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/call_analyzer"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # CORS — comma-separated origins allowed to call the API (the deployed
    # frontend). Local Vite dev server is the default.
    cors_origins: str = "http://localhost:5173"

    @field_validator("database_url")
    @classmethod
    def _asyncpg_scheme(cls, v: str) -> str:
        # Render's managed Postgres hands out postgresql:// (or postgres://);
        # our async stack needs the +asyncpg driver.
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    model_config = {"env_file": _ENV_FILE, "extra": "ignore"}

settings = Settings()