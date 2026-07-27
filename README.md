# Call Analyzer

Upload sales-call recordings (WAV/MP3, up to 30 min), get back a speaker-diarized transcript and a structured LLM analysis — summary, closed-vocabulary tags, customer intent, per-speaker mood — through a React UI. Processing is fully asynchronous: uploads return `202` immediately and the UI polls until each call reaches a terminal state.

**Stack:** FastAPI · arq/Redis · Postgres · Cloudflare R2 (S3 API) · Deepgram Nova-3 · Google Gemini · React (Vite) · Render.

---

## Architecture

```
                        ┌────────────────────┐
                        │   Client (React)   │
                        │  upload · poll     │
                        └──────┬──────▲──────┘
                        upload │      │ poll status
                               ▼      │
                        ┌────────────────────┐
                        │  FastAPI backend   │  returns 202 + call id
                        └──┬──────┬──────┬───┘
                stream file│ insert│      │enqueue
                           ▼      ▼      ▼
                  ┌─────────┐ ┌────────┐ ┌───────────┐
                  │ R2 / S3 │ │Postgres│ │Redis queue│
                  └────▲────┘ └───▲────┘ └─────┬─────┘
          presigned URL│          │write       │pull job
                       │          │results     ▼
                       │      ┌───┴────────────────┐
                       └──────│  Worker (arq)      │
                              └───┬────────────┬───┘
                                  ▼            ▼
                          ┌───────────┐  ┌──────────┐
                          │ Deepgram  │  │  Gemini  │
                          │ STT +     │  │ summary, │
                          │ diarize   │  │tags, mood│
                          └───────────┘  └──────────┘
```

**Call lifecycle (state machine, enforced in code):**
`uploaded → transcribing → analyzing → completed`, with `transcribing/analyzing → failed` on errors and `failed → transcribing` reserved for retry. Illegal transitions raise — a bug can't silently corrupt a row (`backend/app/models/states.py`).

**Worker pipeline is DB-checkpointed.** The transcript and analysis are each committed as they land; a re-run skips any stage whose output already exists. A failure after transcription never re-pays Deepgram.

### Key decisions (and why)

| Decision | Why |
|---|---|
| Async queue (`202` + id), not sync handling | 30-min calls take minutes to process; synchronous handling can't survive timeouts or a 1,000-file burst |
| arq over Celery / FastAPI `BackgroundTasks` | Async-native, minimal boilerplate; `BackgroundTasks` dies with the server and can't scale independently |
| Deepgram owns *who spoke when*; Gemini owns *all semantics* | One authority per judgment type — diarization is signal processing, meaning is language modeling |
| Streaming upload → R2, presigned URL → Deepgram | A ~300 MB WAV never sits in API memory (500 MB cap enforced in-stream); the worker hands Deepgram a URL and never moves audio bytes |
| Structured LLM output via Gemini `response_schema`, `temperature=0` | Output is shape-guaranteed and validated once against our own Pydantic schema — malformed output is an explicit, coded failure, never stored |
| Single `calls` table + JSONB payloads | Transcript/analysis shapes evolved across milestones without a migration each time; query-surface fields (`status`, `created_at`, `error_code`) are real indexed columns |
| Storage behind a `Protocol` | MinIO (offline dev) and R2 (real) differ only in `.env` values — the code is identical |
| Cursor (keyset) pagination on `(created_at, id)` | Stable under concurrent inserts while the list is being polled; no offset-walk cost at depth |
| Polling, only while calls are in flight | Right-sized for the scale; the frontend's timer stops when nothing is processing. SSE is the named production upgrade |

---

## Running locally

Prereqs: Docker, Node 20+, [uv](https://docs.astral.sh/uv/), and API keys for Deepgram and Google Gemini + a Cloudflare R2 bucket.

```bash
cp .env.example .env       # fill in DEEPGRAM_API_KEY, GOOGLE_API_KEY, STORAGE_* (R2)
docker compose up -d       # api :8000 · worker · postgres · redis
cd frontend && npm install && npm run dev   # UI at http://localhost:5173
```

Drop `.wav`/`.mp3` files on the page, watch statuses walk to `completed`, click a call for its transcript and analysis.

> **Why R2 even locally:** the worker hands Deepgram a presigned URL, which Deepgram's cloud must be able to fetch — a MinIO URL on the docker network is unreachable from the internet. For offline work on everything *except* real transcription, run `docker compose --profile minio up` and point the `STORAGE_*` vars at MinIO (values are commented in `.env.example`).

Migrations run automatically in Docker; manually: `cd backend && uv run alembic upgrade head`.

### Smoke scripts (service-level checks without the pipeline)

```bash
cd backend
uv run python scripts/check_deepgram.py <audio> --save   # transcribe one file → transcripts/
uv run python scripts/check_gemini.py transcripts/<stem>_transcript.json --save
```

### Test fixtures

`backend/tests/fixtures/generate_stubs.py` generates 1,000 tiny valid WAVs for bulk-upload testing. **Note:** the stubs are silent — they exercise the upload/queue path but fail real transcription with `no_speech_detected` (by design). Real recordings go in `tests/fixtures/audio/recordings/` (gitignored).

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/calls` (multipart `file`) | Validate WAV/MP3, stream to storage, insert row, enqueue job → `202 {id, status}` |
| `GET /api/calls?status=&cursor=&limit=` | Cursor-paginated list (cheap summary fields only — this is what the UI polls) |
| `GET /api/calls/{id}` | Full record: status, `error_code`, transcript, analysis |
| `GET /health` | Liveness |

## Analysis output (the tagging schema)

Closed vocabularies, defined once as `StrEnum`s in `backend/app/models/analysis_schema.py` — the same definition is Gemini's `response_schema`, the response validator, and the stored JSONB shape:

- `tags.outcome` (exactly one): `meeting_scheduled · info_requested · not_interested · not_qualified · closed_won · no_clear_outcome`
- `tags.objections` (multi, each with a **verbatim quote** as evidence): `price · timing · authority · need · trust · competitor`
- `tags.lead_temperature` (ordinal): `cold · warm · hot`
- `intent`: `ready_to_buy · evaluating · gathering_info · price_shopping · not_interested`
- `mood.agent / mood.customer`: label (`positive · neutral · negative · frustrated`) + a one-line note on how it evolved
- plus `summary` (always English), `next_step`, and `reasoning`

Closed vocabularies mirror how sales ops consumes calls — free-text tags fragment and can't be aggregated. Two design points that materially improved output quality:

1. **`reasoning` is the schema's first field.** Structured output is generated in property order, so the model must justify from transcript evidence *before* it tags — this eliminated internally-inconsistent tag combinations observed in the first iteration.
2. **Objections require a quote.** A tag has to point at an actual line, which either justifies it or exposes it.

Speaker roles (agent vs. customer) are inferred by the LLM from content; diarization only provides anonymous speaker numbers.

**Evaluating tagging quality over time** (methodology, not yet tooled): maintain a small golden set of hand-tagged calls, re-run it on any prompt/model change and track agreement; periodically human-review a sample of production tags; monitor tag distributions for drift. `temperature=0` keeps runs comparable.

## Error handling

Every failure lands the call in `failed` with a machine-readable `error_code` — never a silent death. Provider exceptions are translated into a taxonomy (`backend/app/services/errors.py`) at each service boundary; the worker branches on the class:

| Code | Class | Meaning |
|---|---|---|
| `provider_rate_limited` | retryable | 429 from Deepgram/Gemini |
| `provider_unavailable` | retryable | 5xx / network failure |
| `provider_timeout` | retryable | request exceeded our timeout |
| `provider_auth` | permanent | 401/402/403 — key or billing |
| `audio_unreadable` | permanent | provider rejected the audio |
| `no_speech_detected` | permanent | transcription succeeded, found no speech |
| `analysis_failed` / `analysis_blocked` / `analysis_invalid` | permanent | Gemini failure / safety-blocked / schema-invalid output |
| `enqueue_failed` | permanent | upload stored but queueing failed |
| `internal_error` | permanent | unclassified bug |

Current policy is deliberately simple: `max_tries=1`, failures are surfaced, recovery is re-upload. The retryable/permanent split means automatic backoff and a `POST /calls/{id}/retry` endpoint are drop-in additions (see roadmap below) — and thanks to checkpointing, a retry after transcription would skip Deepgram entirely.

---

## Deployment (Render)

`render.yaml` declares the full topology: **web** (FastAPI, Docker) · **worker** (arq, same image) · **Key Value** (Redis, `noeviction` so queued jobs are never evicted) · **Postgres** · **static site** (Vite build). R2 stays external.

1. Push, then Render → **New → Blueprint** → select this repo.
2. Enter the `sync: false` secrets on api + worker: `DEEPGRAM_API_KEY`, `GOOGLE_API_KEY`, `STORAGE_*` (R2 values).
3. First deploy runs `alembic upgrade head` via `preDeployCommand`. The config layer normalizes Render's `postgresql://` URL to `postgresql+asyncpg://` automatically.
4. Wire the cross-references: API's `CORS_ORIGINS` ← static-site URL; frontend's `VITE_API_BASE_URL` ← API URL, then **redeploy the static site** (Vite bakes env at build time).

> Free-tier services cold-start after idle — the first request after a quiet period is slow. That's the tier, not the app.

## Scaling to 10k calls/day

10k/day ≈ 420/hour, bursty. The architecture handles it by turning knobs, not redesigning: horizontal arq workers (the queue distributes naturally) → presigned **direct-to-R2 uploads** (removes file traffic from the API tier entirely) → Deepgram **callbacks** instead of held connections → **SSE** for status → managed Redis with persistence (the queue is the SPOF) → observability (queue depth, job latency, failure-rate alerts, logs correlated by `call_id`).

**Bottleneck order:** external API quotas (always first — Deepgram allows 50 concurrent pre-recorded requests) → upload bandwidth through the API → Postgres write contention (much later) → Redis SPOF (a risk, not a throughput limit).

**PII** (recordings are PII by definition): encryption at rest (R2, Postgres) + TLS in transit; retention policy — delete raw audio after N days, keep transcripts/analysis; access control and audit logging on reads; Deepgram `redact` for card/number data; data-locality awareness for Mexican consumers (LFPDPPP).

## Deliberate scope cuts & roadmap

Documented choices, not gaps — each was cut to keep the core pipeline production-shaped:

- **Automatic retry/backoff + retry endpoint** — taxonomy and state machine already support them (design in `.claude/milestone_2.md`).
- **Per-provider concurrency semaphore** — at single-worker scale, arq's `max_jobs` (10) already sits under Deepgram's 50-concurrent quota; the semaphore is the multi-worker knob.
- **Tag-override editing & JSON export** — the UI is read-only; model output is stored untouched as ground truth (overrides would live in the separate `tag_overrides` column).
- **Auth / multi-user, SSE, direct-to-R2 uploads, Deepgram webhooks** — single-tenant demo; each named as the production upgrade where relevant.
- **pytest suite** — verification so far is scripted manual checks per milestone (recorded in `.claude/milestone_*_results.md`); the automated suite (state-machine rules, error classification, schema validation, HTTP-mocked integration paths) is the next milestone.

## Repo layout

```
backend/
  app/
    api/         # routes (upload, list, detail) + deps
    core/        # config (pydantic-settings), db engine/session
    models/      # Call ORM, state machine, response + analysis schemas
    services/    # storage (S3 protocol), transcription (Deepgram),
                 # analysis (Gemini), error taxonomy
    worker/      # arq settings + the checkpointed pipeline task
  alembic/       # migrations
  scripts/       # check_deepgram.py, check_gemini.py smoke tools
  tests/fixtures/  # stub generator + (gitignored) real recordings
frontend/        # Vite + React + TS SPA (upload/list/detail)
render.yaml      # Render blueprint (web, worker, key value, postgres, static)
docker-compose.yml
.claude/         # build plan, per-milestone task docs and verification results
```

The commit history narrates the build milestone by milestone; `.claude/` holds the plan, per-task design docs, and recorded verification results for each milestone.
