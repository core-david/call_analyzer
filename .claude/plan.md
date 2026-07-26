# Call Analyzer — Architecture Reference & Build Plan

> Working reference for the Altur take-home. Stack: **FastAPI · arq/Redis · Postgres · Cloudflare R2 · Deepgram · LLM API · React (Vite) · Render**. Must-haves first, bonuses second.

---

## 1. What we're building

A web app that accepts sales-call recordings (WAV/MP3, up to 30 min), transcribes them with speaker diarization (Deepgram), analyzes the transcript with an LLM (summary, tag schema, intent, mood per speaker), persists everything, and exposes it through a list/detail UI. Processing is fully asynchronous: uploads return immediately and the UI polls until each call reaches a terminal state.

## 2. Architecture

```
                        ┌────────────────────┐
                        │   Client (React)   │
                        │  upload · poll     │
                        └──────┬──────▲──────┘
                        upload │      │ poll status
                               ▼      │
                        ┌────────────────────┐
                        │  FastAPI backend   │  returns 202 + job id
                        └──┬──────┬──────┬───┘
                stream file│      │insert│enqueue
                           ▼      ▼      ▼
                  ┌─────────┐ ┌────────┐ ┌───────────┐
                  │ R2 / S3 │ │Postgres│ │Redis queue│
                  │  audio  │ │  jobs, │ │  pending  │
                  └────▲────┘ │results │ │   jobs    │
                       │      └───▲────┘ └─────┬─────┘
          presigned URL│          │write       │pull job
                       │          │results     ▼
                       │      ┌───┴────────────────┐
                       └──────│    Worker pool     │
                              │       (arq)        │
                              └───┬────────────┬───┘
                                  ▼            ▼
                          ┌───────────┐  ┌──────────┐
                          │ Deepgram  │  │ LLM API  │
                          │ STT +     │  │ summary, │
                          │ diarize   │  │tags, mood│
                          └───────────┘  └──────────┘
```

**Job lifecycle (state machine):**
`uploaded → transcribing → analyzing → completed`, with `transcribing/analyzing → failed` on permanent errors and `failed → transcribing` on user-triggered retry. Illegal transitions are rejected in code, not by convention.

**Render topology:** Web Service (FastAPI) · Background Worker (arq) · Key Value (Redis) · Postgres · Static Site (frontend). Object storage on Cloudflare R2. Local development mirrors all of it via docker-compose (api, worker, redis, postgres, minio).

## 3. Key decisions

| Decision | Choice | Why |
|---|---|---|
| Processing model | Async queue, `202 Accepted` + job id | 30-min calls take minutes to process; synchronous handling can't survive timeouts or bursts |
| Queue | arq (Redis) | Async-native, matches FastAPI; far less boilerplate than Celery. FastAPI `BackgroundTasks` rejected: in-process, dies with the server, can't scale independently |
| STT | Deepgram (Nova) | Native diarization (bonus #1), handles long files by URL, no 25MB chunking problem (Whisper's limit), language detection for es/en calls |
| LLM | Google Gemini 2.5 Flash (`google-genai` SDK) | Free tier (500 RPD, 10M TPD), native `response_schema` for guaranteed JSON schema compliance, strong bilingual en/es, fast inference |
| Judgment split | Deepgram owns *who spoke and when*; LLM owns *all semantics* (summary, tags, intent, mood) | One authority per judgment type; diarization is signal processing, meaning is language modeling |
| LLM output | Structured JSON, temperature 0, Pydantic-validated, one repair retry | Malformed output is the main failure mode; validation makes it explicit and testable |
| Tagging schema | Closed vocabularies: one mandatory `outcome`, multi-label `objections`, ordinal `lead_temperature` | Mirrors how sales ops consumes calls; free-text tags fragment and can't be aggregated |
| Persistence | Single `calls` table + JSONB columns | Right-sized for the scope; normalization is a production note, not a weekend task |
| Tag edits | `tag_overrides` stored separately from model `tags` | Never destroy model output — it's the ground truth for evaluating tagging quality over time |
| Audio storage | R2 with presigned URLs | Render services don't share a disk; presigned URL means the worker hands Deepgram a link and never moves audio bytes itself |
| Worker design | One task, DB-checkpointed stages | If the LLM fails, retry skips transcription (transcript already persisted) — no double-paying Deepgram |
| Status updates | Polling (3–5s, only while calls are in flight) | Right-sized; SSE named as the production upgrade |
| Frontend | Vite SPA, not Next.js | No SSR/SEO need; API layer already exists in FastAPI; static deploy is free and simple |
| Errors | Classified retryable (429/5xx/timeout → backoff, max 3) vs permanent (corrupt file, 4xx → `failed` + machine-readable `error_code`) | Jobs never die silently; every failure is visible and actionable in the UI |

## 4. Decoupling decisions

- **API ↔ processing:** the queue is the seam. The API only writes (file → R2, row → Postgres, job → Redis) and returns; it knows nothing about how processing happens. Either side can be scaled, restarted, or rewritten without touching the other.
- **Orchestration ↔ external services:** the worker task orchestrates but never calls Deepgram or the LLM directly; that lives in a `services/` layer (`transcription.py`, `analysis.py`). This is what makes the pipeline testable with HTTP-level mocks and providers swappable.
- **Storage behind a protocol:** `Storage` interface with `S3Storage` (R2/MinIO) and `LocalStorage` implementations. Local dev, tests, and production differ only in configuration.
- **Frontend ↔ backend:** plain REST + polling. The SPA has no privileged knowledge of the pipeline; it renders whatever `status` says.
- **Concurrency shaping is layered:** the queue absorbs bursts (unbounded intake), `max_jobs` caps work per worker, and per-provider semaphores cap concurrent external calls. Each knob is independent.

## 5. The two defining constraints

**Calls up to 30 minutes.** Upload streams to R2 in chunks — a ~300MB WAV is never materialized in API memory. The endpoint returns `202` in milliseconds regardless of file size. The worker passes Deepgram a presigned URL, so large files never transit the worker. Checkpointing means a mid-pipeline failure on a long call never repeats the expensive transcription step.

**1,000 recordings at once.** The frontend uploads with a client-side concurrency pool (~5 in flight); each upload is independent, so partial failure is per-file, not per-batch. The API's work per upload is trivial, so 1,000 rows and 1,000 queue entries land in a couple of minutes. Workers then drain the queue at a rate shaped by semaphores to respect Deepgram/LLM quotas — burst absorbed, drain controlled (backpressure). Cursor pagination keeps the polled list view fast as rows accumulate.

## 6. Non-goals (deliberate, documented)

- **Auth / multi-user** — single-tenant demo; noted as the first production addition.
- **Real-time push (SSE/WebSockets)** — polling suffices at this scale; upgrade path named.
- **Direct-to-R2 presigned uploads** — production pattern, unnecessary complexity for the demo.
- **Deepgram callback/webhook mode** — awaited requests are fine at demo volume.
- **Analytics dashboard** — cut from scope (export + overrides chosen instead).
- **Audio playback in the UI** — nice-to-have, not evaluated.
- **HA Redis / multi-region / read replicas** — production notes only.
- **Automated tagging-eval pipeline** — the *methodology* (golden set, agreement, drift monitoring) is documented; the tooling is not built.

## 7. Scaling to 10k calls/day and beyond

10k/day ≈ 420/hour, bursty. The architecture already handles it by turning knobs, not redesigning:

1. **Horizontal workers** — add arq instances; the queue distributes work naturally.
2. **Presigned direct uploads** (browser → R2) — removes file traffic from the API tier entirely, which then scales as a stateless metadata service.
3. **Deepgram callbacks** instead of held connections — workers stop waiting on transcription and throughput per worker jumps.
4. **SSE for status** — kills the polling load that grows linearly with active users.
5. **Managed Redis with persistence** — the single queue is the SPOF; fix it before it matters.
6. **Observability** — structured logs correlated by `call_id`, queue-depth and job-latency metrics, alerting on failure rate. You cannot operate a queue you cannot see.

**Bottleneck order:** external API quotas (first, always) → upload bandwidth through the API → Postgres write contention (much later) → Redis SPOF (a risk, not a throughput limit).

**On rewriting ingestion in Go:** at 100k+/day, or if the upload path becomes the bound, a small Go ingest service is the textbook move — goroutines give cheap per-connection concurrency, memory per streaming upload is tiny, and multipart handling is fast. Python would keep everything orchestration- and ML-adjacent (workers, prompts, analysis). Honest caveat to state in interviews: presigned direct uploads eliminate most of the file-handling load *before* a rewrite is justified — Go is the answer when the API tier itself must touch every byte at high volume, not the default next step.

**PII (recordings are PII by definition):** encryption at rest (R2, Postgres) and TLS in transit; retention policy — delete raw audio after N days, keep transcripts/analysis; access control + audit logging on reads; Deepgram `redact` option for numbers/PCI data; data-locality awareness for Mexican consumers (LFPDPPP).

---

## 8. Milestones

**M0 — Environment and project foundation**

*Goal:* remove every source of setup friction before real work begins, so build sessions are spent building, not signing up for services.

Create accounts and obtain API keys for Deepgram, the LLM provider, Render, and Cloudflare R2, and verify Docker runs locally. Initialize the git repository with the agreed folder structure and a README stub as the first meaningful commit. Gather three or four test recordings, including at least one 20–30 minute file — realistic test audio is the asset everyone forgets until it blocks them. Resolve the Render worker-tier question (paid background worker vs. running the arq worker as a second process inside the web service container) so the deployment approach is settled before anything depends on it.

*Expected result:* repository exists with its first commit, every credential sits in a local `.env`, test audio is on disk, and the deployment approach is decided.

**M1 — Stubbed end-to-end skeleton**

*Goal:* prove the entire asynchronous architecture works with fake data before touching any external service, so integration problems and architecture problems can never be confused with each other.

Stand up the local environment with docker-compose (api, worker, redis, postgres, minio) — the worker boots with a no-op `ping` task, since arq requires at least one registered function. Create the database schema and first migration. Build the upload endpoint: validate the file type, stream the file to storage without holding it in memory, insert the call row, enqueue the job, return `202` with the id. Build the worker task so it walks the full state machine using stubbed transcription and analysis functions that return canned data. Build the list and detail endpoints with cursor pagination. Enforce legal state transitions in code.

*Expected result:* uploading a file through the API visibly moves it `uploaded → transcribing → analyzing → completed` when polled, with canned results persisted; restarting the services does not lose queued jobs. Every later milestone is now a substitution of a stub for a real service, with the architecture already proven.

**M2 — Real transcription (Deepgram)**

*Goal:* replace the transcription stub with a production-shaped integration that survives long files and provider failures.

Implement presigned URL generation from object storage and the Deepgram request with diarization, utterances, smart formatting, and language detection. Persist the diarized transcript in the agreed shape (speaker, start, end, text) plus a flat text version. Implement error classification — timeouts, rate limits, and server errors retry with backoff up to a capped number of attempts; corrupt audio and client errors go straight to `failed` with a machine-readable error code. Add the per-provider concurrency semaphore. Verify the whole path against the long test recording, and confirm the transcript checkpoint: once a transcript is persisted, any later failure and retry must skip transcription entirely.

*Expected result:* a real upload of a 20–30 minute call produces a speaker-labeled transcript in the database; simulated provider failures land in the correct path (retried, or failed with the right error code); a retry after the transcript exists provably does not call Deepgram again.

**M3 — LLM analysis**

*Goal:* turn a diarized transcript into structured, validated judgments — summary, tags, customer intent, and per-speaker mood — with the reasoning behind the tagging schema documented as it is designed.

Define the analysis output schema: one mandatory call outcome, multi-label objections, ordinal lead temperature, customer intent, and mood per speaker. Design the prompt around the diarized transcript, including the mapping of anonymous speaker labels to roles inferred from content. Validate every response strictly, with a single repair retry that feeds the validation error back before marking the call failed. Exercise the edge cases: a bilingual call, a one-sided call, a transcript where diarization clearly went wrong. Write the tagging-schema justification and the plan for evaluating tagging quality over time (golden set, periodic human review, distribution drift) into the README now, while the reasoning is fresh.

*Expected result:* completed calls carry a summary, schema-valid tags, and analysis; malformed model output is caught and either repaired or failed visibly, never stored; the README section justifying the tagging schema and its evaluation is drafted.

**M4 — Frontend and remaining endpoints**

*Goal:* make the pipeline usable by a person, designed around the three real workflows — upload a batch, monitor progress, review a call.

Build the upload view with multi-file drag-and-drop and a client-side concurrency pool so a large batch uploads a few files at a time, each with independent success or failure. Build the list view with status badges, filtering by status, and disciplined polling that runs only while any visible call is still in flight. Build the detail view: summary card, tags with editing, and the transcript rendered as speaker-distinguished utterances with timestamps. Add the two remaining backend endpoints — tag overrides (stored separately from model output) and JSON export of a full call record — and wire them into the detail view.

*Expected result:* the full workflow works in a browser: drop in many files at once, watch statuses progress live, open any call to read its transcript and analysis, correct its tags, and download the record as JSON.

**M5 — Tests and hardening**

*Goal:* demonstrate a deliberate testing strategy and flush out real bugs while there is still time to fix them — which is why this milestone runs before deployment, not after.

Write unit tests for the pure logic: state-transition rules, error classification, and analysis-schema validation against both valid and malformed inputs. Write integration tests with the external providers mocked at the HTTP layer, covering the happy path, a corrupt upload, a provider failure ending in `failed` with the right error code, and the checkpoint behavior on retry. Fix whatever these surface.

*Expected result:* the suite runs with a single command, covers the important logic and the failure paths rather than trivialities, and the README describes it as a strategy — what is tested at which level and why.

**M6 — Deployment**

*Goal:* a live, clickable instance a reviewer can use without installing anything.

Provision the Render services (web, worker, Redis, Postgres, static site) and the R2 bucket. Configure environment variables, run migrations against the managed database, and sort out CORS between the static frontend and the API. Smoke test the live URL with a real recording end to end. Note the free-tier cold-start behavior in the README so a reviewer isn't confused by a slow first load.

*Expected result:* a public URL where uploading a recording produces a full transcript and analysis; the deployment is reproducible from the README alone.

**M7 — Documentation and final review**

*Goal:* make the reasoning legible — documentation is a scored dimension of this challenge, equal in weight to the code.

Complete the README: local and Docker setup, how to test, required environment variables, assumptions made, the architecture decisions and their trade-offs, and what would be improved with more time. Write ARCHITECTURE.md answering the four scale questions: path to 10k calls/day, bottleneck order, production changes, and PII handling. Read the commit history start to finish as a reviewer would — it should narrate the build. Do a final click-through of the live app and the local docker-compose path.

*Expected result:* a reviewer can clone, run, test, and understand every major decision without asking a single question; the submission is ready to send.

---

**Slip plan (cut in order):** tag-overrides UI (the endpoint stays) → JSON export → live deployment (docker-compose alone satisfies the must-haves). **Never cut M5 or M7** — tests and documentation are scored dimensions; deployment is not a must-have.
