from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Deepgram
    deepgram_api_key: str = ""

    # Google Gemini
    google_api_key: str = ""

    # Storage (R2 in prod, MinIO locally)
    storage_endpoint_url: str = "http://localhost:9000"
    storage_access_key: str = "minioadmin"
    storage_secret_key: str = "minioadmin"
    storage_bucket_name: str = "call-analyzer-audio"

    # Postgres
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/call_analyzer"

    # Redis
    redis_url: str = "redis://localhost:6379"

    model_config = {"env_file": ".env", "extra": "ignore"}

settings = Settings()