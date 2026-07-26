# Milestone 0 — Task Implementation Guide

> Each task includes what to explain before coding, then the implementation steps.
> Task 0.1 is complete (accounts provisioned). `.env` creation folded into 0.3.

---

## Task 0.3 — Initialize repository & folder structure

### Explain first

We're creating the skeleton that every future task lands in. The layout follows a mono-repo pattern: `backend/` for Python (FastAPI + arq worker) and `frontend/` for React (scaffolded in M4). Every Python package directory gets an `__init__.py` so imports work. Non-Python directories that need to exist in git get `.gitkeep` (git doesn't track empty dirs).

The `.env.example` lists every variable the app will need with dummy values — it's the contract between the codebase and whoever deploys it. The real `.env` is gitignored and the user fills it in from their account credentials.

### Steps

1. `git init` in the project root (`/Users/david/Developer/call_analyzer`)

2. Create directory tree:
   ```
   backend/
     app/
       __init__.py
       api/__init__.py
       worker/__init__.py
       services/__init__.py
       models/__init__.py
       core/__init__.py
     tests/
       __init__.py
       fixtures/.gitkeep
   frontend/.gitkeep
   docs/.gitkeep
   ```

3. Create `.gitignore`:
   - Python: `__pycache__/`, `*.pyc`, `.venv/`, `*.egg-info/`
   - Node: `node_modules/`, `dist/`
   - Env: `.env`, `.env.local`
   - IDE: `.vscode/`, `.idea/`
   - OS: `.DS_Store`
   - Audio fixtures: `tests/fixtures/audio/`
   - Docker: volumes, overrides

4. Create `.env.example`:
   ```env
   # Deepgram
   DEEPGRAM_API_KEY=your-deepgram-key

   # Google Gemini
   GOOGLE_API_KEY=your-google-api-key

   # Cloudflare R2
   R2_ACCOUNT_ID=your-account-id
   R2_ACCESS_KEY_ID=your-access-key
   R2_SECRET_ACCESS_KEY=your-secret-key
   R2_BUCKET_NAME=call-analyzer-audio
   R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com

   # Postgres (local defaults)
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/call_analyzer

   # Redis (local defaults)
   REDIS_URL=redis://localhost:6379

   # MinIO (local S3-compatible storage)
   STORAGE_ENDPOINT_URL=http://localhost:9000
   STORAGE_ACCESS_KEY=minioadmin
   STORAGE_SECRET_KEY=minioadmin
   STORAGE_BUCKET_NAME=call-analyzer-audio
   ```

5. Create `.env` — copy of `.env.example`. Prompt user to fill in their real keys for `DEEPGRAM_API_KEY`, `GOOGLE_API_KEY`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT_URL`.

6. Create `README.md` stub — project name, one-line description, "Setup instructions coming in M7."

7. Create `Makefile` stub with placeholder targets:
   ```makefile
   .PHONY: dev test lint

   dev:           ## Start local environment
   	docker compose up --build

   test:          ## Run test suite
   	cd backend && uv run pytest

   lint:          ## Lint and format
   	cd backend && uv run ruff check . && uv run ruff format .
   ```

8. First commit: everything above.

### Files created
- `.gitignore`, `.env.example`, `.env`, `README.md`, `Makefile`
- All `__init__.py` and `.gitkeep` files in the directory tree

---

## Task 0.4 — Python project & dependency scaffold

### Explain first

`pyproject.toml` is the single source of truth for the Python project — name, version, Python version constraint, all dependencies, and dev dependency groups. `uv` reads this, resolves versions, and writes `uv.lock` for reproducible installs. No `requirements.txt` files.

`config.py` uses `pydantic-settings` to load environment variables with type validation and defaults. Every config value used anywhere in the app flows through this single `Settings` class. Defaults are set for local development (postgres on localhost:5432, redis on localhost:6379, minio on localhost:9000) so running locally without Docker works out of the box.

`main.py` is the FastAPI application entry point. For now it has a single `GET /health` endpoint returning `{"status": "ok"}`. This exists so we can verify the app boots (locally and in Docker) before adding real routes.

### Steps

1. Initialize the project with uv:
   ```bash
   cd backend
   uv init --python 3.12
   ```

2. Add production dependencies one by one (uv resolves and updates `pyproject.toml` + `uv.lock` automatically):

   ```bash
   uv add fastapi "uvicorn[standard]" arq "sqlalchemy[asyncio]" asyncpg alembic httpx aioboto3 deepgram-sdk google-genai pydantic-settings python-multipart
   ```

3. Add dev dependencies:
   ```bash
   uv add --dev pytest pytest-asyncio ruff
   ```
Dev dependencies are packages only needed during development — not in production. Things like:    
  - pytest — running tests
  - ruff — linting/formatting                         
  uv add --dev puts them in a separate [dependency-groups] section in pyproject.toml. When deploying to production, you can install without them (uv sync --no-dev) to keep 
  the image smaller and avoid unnecessary packages.  





4. Configure tool settings in `pyproject.toml` (append manually after uv init):
   ```toml
   [tool.ruff]
   target-version = "py312"
   line-length = 100

   [tool.pytest.ini_options]
   asyncio_mode = "auto"
   ```

3. Create `backend/app/core/config.py`:
   ```python
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
   ```

4. Create `backend/app/main.py`:
   ```python
   from fastapi import FastAPI

   app = FastAPI(title="Call Analyzer")

   @app.get("/health")
   async def health():
       return {"status": "ok"}
   ```

5. Verify locally: `cd backend && uv run uvicorn app.main:app --port 8000` then `curl localhost:8000/health`.

6. Commit.

### Files created
- `backend/pyproject.toml`, `backend/uv.lock`
- `backend/app/core/config.py`, `backend/app/main.py`

---

## Task 0.2 — Gather test audio recordings

### Explain first

We need two kinds of test audio:

**Real recordings (3-4 files)** — for verifying transcription quality and diarization. These are manually downloaded, gitignored, and documented in a README. Sources:
- [CallHome English](https://catalog.ldc.upenn.edu/LDC97S42) — telephone conversations, multi-speaker (requires LDC access, may not be free)
- YouTube sales call role-plays under Creative Commons — practical and free
- [LibriSpeech](https://www.openslr.org/12) — multi-speaker read speech, free, easy to download (not sales calls but fine for testing diarization)
- Record two short calls ourselves if licensing is a concern

We'll document recommended download URLs and let the user fetch them.

**1,000 WAV stubs** — for testing the upload pipeline handles bulk ingestion. These are 1-second, 16-bit, mono, 16kHz silent WAV files. Tiny (~32KB each, ~32MB total) but valid enough to pass MIME-type and header checks. Generated by a script, never committed.

The generator uses Python's stdlib `wave` module — zero external dependencies.

### Steps

1. Create `backend/tests/fixtures/README.md`:
   - Document 3-4 recommended recordings with download instructions
   - Include license info for each source
   - Note that audio files are gitignored

2. Create `backend/tests/fixtures/generate_stubs.py`:
   ```python
   """Generate N valid WAV stub files for bulk upload testing.

   Usage: python generate_stubs.py [--count 1000] [--output-dir ./audio]
   """
   ```
   - Uses `wave` module to write 1-second silent 16-bit mono 16kHz WAV files
   - Accepts `--count` (default 1000) and `--output-dir` (default `./audio`)
   - Names files `stub_0001.wav` through `stub_1000.wav`
   - Prints summary: count, total size, path

3. Create `backend/tests/fixtures/audio/` directory (gitignored via `.gitignore` already).

4. Verify: `cd backend && uv run python tests/fixtures/generate_stubs.py --count 5` — should produce 5 WAV files that play as silence.

5. Commit (script + README only, no audio files).

### Files created
- `backend/tests/fixtures/README.md`
- `backend/tests/fixtures/generate_stubs.py`

---

## Task 0.5 — docker-compose local environment

### Explain first

Six services in `docker-compose.yml`:

| Service | Image | Purpose |
|---------|-------|---------|
| **api** | Built from `backend/Dockerfile` | FastAPI on port 8000, `--reload` for dev |
| **worker** | Same image, different command | arq worker (stub for now — connects to Redis, runs no tasks) |
| **postgres** | `postgres:16-alpine` | Database, port 5432 |
| **redis** | `redis:7-alpine` | Job queue, port 6379 |
| **minio** | `minio/minio` | S3-compatible object storage, port 9000 (API) + 9001 (console) |
| **minio-init** | `minio/mc` | One-shot: creates the bucket, then exits |

1. api — the FastAPI web server

- Built from backend/Dockerfile (your own image with the app code).
- Runs uvicorn serving app.main:app on port 8000 — this is what handles HTTP requests: the /health endpoint now, and later the upload/results endpoints.
- Uses --reload in dev, and mounts ./backend:/app as a volume so editing code on your machine instantly restarts the server (no rebuild needed).
- This is the only service the browser/frontend talks to.

2. worker — the background job processor

- Same image as api, but the command is overridden to run arq instead of uvicorn.
- arq is a Redis-backed async task queue. When a call gets uploaded, the API doesn't transcribe it inline (that's slow — Deepgram + Gemini take seconds to minutes). Instead it drops a job on Redis, returns immediately, and the worker picks it up and does the heavy lifting.
- Right now it's a stub — it connects to Redis but has no tasks (functions = []). Real tasks come in M1.
- No exposed port — nothing connects to it; it only pulls jobs from Redis.

3. postgres — the database

- Image: postgres:16-alpine (alpine = tiny Linux base).
- Port 5432. Stores your application data (call records, transcripts, analysis results — schema comes later).
- Has a named volume so data survives docker compose down and container restarts.
- Health check via pg_isready so dependent services wait until it's actually accepting connections.

4. redis — the job queue broker

- Image: redis:7-alpine.
- Port 6379. In-memory data store; here it's the message bus between api (enqueues jobs) and worker (consumes jobs).
- Health check via redis-cli ping.

5. minio — local S3-compatible object storage

- Image: minio/minio.
- Stores the actual audio files. In production you'll use Cloudflare R2; MinIO is the local stand-in that speaks the same S3 API, so your storage code is identical in both environments (just swap the endpoint URL).
- Two ports: 9000 (S3 API the app uses) and 9001 (a web console you can open in a browser to browse buckets — login minioadmin/minioadmin).
- Health check hits /minio/health/live.

6. minio-init — one-shot bucket creator

- Image: minio/mc (MinIO's CLI client).
- Not a long-running service — it starts, waits for minio to be healthy, creates the call-analyzer-audio bucket, then exits. restart: "no" keeps it from looping.
- Without it, you'd have to manually create the bucket every fresh start. This automates it.

---
How they fit together:

browser/frontend
      │  HTTP :8000
      ▼
   ┌─────┐  enqueue job   ┌───────┐   pull job   ┌────────┐
   │ api │ ─────────────► │ redis │ ◄─────────── │ worker │
   └──┬──┘                └───────┘              └───┬────┘
      │                                             │
      │ read/write records                          │ store/fetch audio
      ▼                                             ▼
 ┌──────────┐                                  ┌─────────┐
 │ postgres │                                  │  minio  │◄── minio-init (creates bucket, exits)
 └──────────┘                                  └─────────┘

The depends_on + health checks enforce startup order: postgres/redis/minio must be healthy, and minio-init must finish, before api and worker start — so nothing races against a database or bucket that isn't ready yet.

The `backend/Dockerfile` installs `uv`, copies the project, syncs dependencies, and runs uvicorn by default. For the worker, docker-compose overrides the command to run arq instead.

`env_file: .env` in docker-compose means all services read the same `.env` — one source of truth. The local-dev defaults in `config.py` are overridden by docker-compose environment variables where hostnames differ (e.g., `postgres` instead of `localhost`).

`depends_on` with health checks ensures api/worker don't start until postgres, redis, and minio are healthy. The minio-init container waits for minio's health check, creates the bucket, and exits.

The worker needs a minimal entry point — `backend/app/worker/settings.py` with an empty arq `WorkerSettings` that connects to Redis. No tasks defined yet (that's M1).

### Steps

1. Create `backend/app/worker/settings.py`:
   ```python
   """arq worker settings. Tasks added in M1."""
   from app.core.config import settings

   class WorkerSettings:
       redis_settings = settings.redis_url
       functions = []
   ```
   (Exact arq config syntax to be verified against arq docs during implementation.)

2. Create `backend/Dockerfile`:
   - Base: `python:3.12-slim`
   - Install `uv` via pip
   - Copy `pyproject.toml` + `uv.lock` first (cache layer)
   - `uv sync --frozen` (install without updating lock)
   - Copy `app/` source
   - Default CMD: `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000`

3. Create `docker-compose.yml`:
   - All services on a `call-analyzer` network
   - Postgres: volume for data persistence, health check via `pg_isready`
   - Redis: health check via `redis-cli ping`
   - MinIO: health check via `curl -f http://localhost:9000/minio/health/live`
   - MinIO-init: `entrypoint` runs `mc alias set local http://minio:9000 minioadmin minioadmin && mc mb --ignore-existing local/call-analyzer-audio`, depends on minio healthy, `restart: "no"`
   - API: `env_file: .env`, env override `DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/call_analyzer`, `REDIS_URL=redis://redis:6379`, `STORAGE_ENDPOINT_URL=http://minio:9000`. Mounts `./backend:/app` for hot reload. Ports `8000:8000`. Depends on postgres, redis, minio-init.
   - Worker: same image + env + volume as API, command overridden to `uv run arq app.worker.settings.WorkerSettings`. No port. Depends on postgres, redis.

4. Start Docker Desktop (user must do this manually).

5. Run `docker compose up --build` and verify:
   - All containers start and reach healthy state
   - `curl localhost:8000/health` returns `{"status": "ok"}`
   - MinIO console accessible at `localhost:9001` (login: minioadmin/minioadmin)
   - `call-analyzer-audio` bucket exists in MinIO
   - Worker container is running (logs show arq startup, no errors)

6. Commit.

### Files created
- `backend/Dockerfile`
- `backend/app/worker/settings.py`
- `docker-compose.yml`

### Implementation notes (done 2026-07-24)

Implemented and verified against Docker 29.3.0. Deviations from the plan above,
each necessary:

1. **arq requires ≥1 registered function.** The planned stub `functions = []`
   crashes on boot in arq 0.28 (`RuntimeError: at least one function or cron_job
   must be registered`). Registered a no-op `ping` task so the worker is a valid
   stub until M1. Also, `redis_settings` must be a `RedisSettings` object, not a
   URL string — used `RedisSettings.from_dsn(settings.redis_url)`.

   ```python
   from arq.connections import RedisSettings
   from app.core.config import settings

   async def ping(ctx: dict) -> str:
       return "pong"

   class WorkerSettings:
       redis_settings = RedisSettings.from_dsn(settings.redis_url)
       functions = [ping]
   ```

2. **MinIO healthcheck uses `curl`, not `mc ready local`.** Verified the
   `minio/minio` image ships both `curl` and `mc`, but `mc ready local` needs an
   alias that isn't configured inside the server container. Used
   `curl -f http://localhost:9000/minio/health/live` (self-contained, matches
   the plan's intent).

3. **Anonymous volume for `/app/.venv`.** The `./backend:/app` bind mount would
   otherwise shadow the container's Linux venv with the host's macOS venv,
   breaking `uv run`. Added `- /app/.venv` to the api/worker volume lists.

4. **`backend/.dockerignore` added** (not in the original plan) to keep `.venv`,
   `tests/fixtures/audio/`, and `.env` out of the build context.

5. **Dockerfile installs uv by copying from the official image**
   (`COPY --from=ghcr.io/astral-sh/uv:latest`) rather than `pip install uv`, and
   uses `uv sync --frozen --no-dev` for a lean production image.

**Verification results:** all six services start in dependency order;
`curl localhost:8000/health` → `{"status":"ok"}`; postgres/redis/minio reach
healthy; minio-init exits 0 after creating the `call-analyzer-audio` bucket;
worker logs `Starting worker for 1 functions: ping` and connects to Redis.

**Extra file created:** `backend/.dockerignore`.

---

## Task 0.6 — Resolve Render deployment topology

### Explain first

This is a decision, not code. We need to document how the app deploys to Render, specifically whether the arq worker gets its own Render service or runs alongside the API in a single container.

**Decision: single container, dual process (free-tier compatible).**

Reasoning:
- Render's free tier only supports Web Services, not Background Workers
- A startup script runs both `uvicorn` and `arq` in one container
- docker-compose already proves they can run independently — the single-container deploy is a cost optimization, not an architecture choice
- Document the production upgrade path: separate services with independent scaling

### Steps

1. Create `docs/decisions/001-render-topology.md`:
   - State the decision clearly
   - List the tradeoffs (cost, restart coupling, scaling limitations)
   - Describe the production upgrade path
   - Note that docker-compose proves the separation works

2. Commit.

### Files created
- `docs/decisions/001-render-topology.md`

---

## Execution order summary

```
0.3 Init repo ──→ 0.4 Python scaffold ──→ 0.2 Test audio ──→ 0.5 docker-compose ──→ 0.6 Render decision
                                                                      │
                                                              (start Docker Desktop
                                                               before this step)
```

Each task ends with a commit. User fills in `.env` keys after task 0.3.
