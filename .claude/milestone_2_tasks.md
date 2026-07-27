# Milestone 2 — Task Implementation Guide

> Companion to `milestone_2.md`. Each task: concepts explained first (this doc
> doubles as a learning reference), then the implementation with code, then a
> manual verify step. No pytest in M2 — tests are M5.
>
> **Decisions locked in before writing this doc:**
> - Language: `detect_language=true` (one dominant language per call).
>   `language=multi` (word-level code-switching) is the documented upgrade if
>   heavily mixed es/en calls surface as a problem in M3.
> - Local M2 verification runs against **real R2** — Deepgram's cloud cannot
>   fetch presigned URLs that point at MinIO on a docker network. The storage
>   seam makes this a `.env`-only switch; MinIO stays for offline/stub work.
> - SDK: official **`deepgram-sdk` v7** (`AsyncDeepgramClient`). Most online
>   tutorials show the v3 API (`PrerecordedOptions`) — that surface is gone.
> - `duration` lives inside the transcript JSON — no migration in M2.
> - A 200 response with an empty transcript (silent audio) is a **permanent
>   failure**: `no_speech_detected`. An unanalyzable call must be visible.
> - Fault injection: dev-only `FAULT_INJECT_TRANSCRIPTION` /
>   `FAULT_INJECT_ANALYSIS` env vars, checked at call time, unset in prod.
> - Backoff `5s → 25s → 125s`, `max_tries=3`. Semaphore default **5**
>   (Deepgram pay-as-you-go allows 50 concurrent pre-recorded requests).
> - The retry endpoint owns the `failed → transcribing` transition; a second
>   click sees `transcribing` and gets `409` — that *is* the dedupe guard.
> - No separate `retries_exhausted` code: `failed` + a retryable-class code
>   means "gave up after backoff", documented rather than duplicated.

---

## Task 2.1 — Error taxonomy

### Explain first

**Why errors become types, not strings.** The worker has to *branch* on failure
class: retryable → back off and re-run, permanent → mark `failed` and stop.
Branching on exception **type** (`except RetryableProviderError`) is
checked by the interpreter; branching on string codes is stringly-typed
guesswork. The machine-readable `error_code` rides *on* the exception, so one
object carries both the control-flow decision and what the UI will display.

**Why the module is provider-agnostic.** `errors.py` defines the vocabulary and
the shape; it never imports Deepgram or Gemini. Each *service* owns translating
its provider's failures into the taxonomy — the service is the only layer that
knows what a Deepgram 400 *means* (unreadable audio) vs. what a Gemini 400
would mean (rejected prompt). This mirrors the M1 seam rule: orchestration
(worker) sees only domain types, never SDK exceptions.

**The one generic thing: HTTP status mapping.** Both providers speak HTTP, and
the *transport-level* meaning of a status is universal: `429` is always "slow
down" (retryable), `5xx` is always "their problem" (retryable), `401/402/403`
is always "your credentials/account" (permanent — retrying can't fix a bad API
key). Only the residual 4xx is provider-specific, so the helper takes the
provider's word for what *that* means via `permanent_code`.

**The closed code vocabulary.**

| code | class | meaning |
|---|---|---|
| `provider_rate_limited` | retryable | 429 — quota/concurrency exceeded |
| `provider_unavailable` | retryable | 5xx / network failure / injected transient |
| `provider_timeout` | retryable | request exceeded our timeout |
| `provider_auth` | permanent | 401/402/403 — key, billing, permissions |
| `audio_unreadable` | permanent | provider rejected the audio (4xx) |
| `no_speech_detected` | permanent | transcription succeeded but found no speech |
| `provider_response_invalid` | permanent | 200 with a shape we can't map |
| `internal_error` | permanent | unclassified bug — the honest catch-all |
| `enqueue_failed` | permanent | (from M1) upload stored but queueing failed |

### Steps

1. Create `backend/app/services/errors.py`:

   ```python
   """Provider-agnostic error taxonomy.

   Services translate their provider's failures into these two classes; the
   worker branches on the class (retry vs. fail) and persists `error_code`.
   This module never imports a provider SDK — that translation belongs to
   the service that owns the provider.
   """


   class ProviderError(Exception):
       """Base: a failure talking to an external provider."""

       def __init__(self, error_code: str, detail: str = "") -> None:
           self.error_code = error_code
           super().__init__(f"{error_code}: {detail}" if detail else error_code)


   class RetryableProviderError(ProviderError):
       """Transient — worth retrying with backoff (429, 5xx, timeouts)."""


   class PermanentProviderError(ProviderError):
       """Definitive — retrying cannot help (bad audio, bad credentials)."""


   def classify_http_status(
       status: int | None, *, permanent_code: str, detail: str = ""
   ) -> ProviderError:
       """Map an HTTP status from any provider into the taxonomy.

       Transport-level meanings are universal (429 = slow down, 5xx = their
       problem, 401/402/403 = your account). Only the residual 4xx is
       provider-specific: `permanent_code` names what a rejection means for
       this provider (transcription: `audio_unreadable`).
       `status=None` means the error had no status at all (connection-level)
       — treated as retryable.
       """
       if status in (401, 402, 403):
           return PermanentProviderError("provider_auth", detail)
       if status == 429:
           return RetryableProviderError("provider_rate_limited", detail)
       if status is None or status == 408 or status >= 500:
           return RetryableProviderError("provider_unavailable", detail)
       return PermanentProviderError(permanent_code, detail)
   ```

2. Verify in a REPL:

   ```bash
   cd backend && uv run python -c "
   from app.services.errors import *
   e = classify_http_status(429, permanent_code='audio_unreadable')
   assert isinstance(e, RetryableProviderError) and e.error_code == 'provider_rate_limited'
   e = classify_http_status(400, permanent_code='audio_unreadable')
   assert isinstance(e, PermanentProviderError) and e.error_code == 'audio_unreadable'
   e = classify_http_status(503, permanent_code='x')
   assert isinstance(e, RetryableProviderError)
   e = classify_http_status(401, permanent_code='x')
   assert isinstance(e, PermanentProviderError) and e.error_code == 'provider_auth'
   e = classify_http_status(None, permanent_code='x')
   assert isinstance(e, RetryableProviderError)
   print('taxonomy OK')
   "
   ```

3. Commit.

### Files created
- `backend/app/services/errors.py`

---

## Task 2.2 — Deepgram integration

### Explain first

**The v7 SDK surface.** `AsyncDeepgramClient` exposes
`client.listen.v1.media.transcribe_url(url=..., model=..., ...)` — request
options are plain kwargs mirroring the REST query params, API failures raise
`deepgram.core.api_error.ApiError` carrying `.status_code` and `.body`. The
SDK is built on httpx, so connection-level failures surface as httpx
exceptions — both families get translated into the 2.1 taxonomy at this
boundary, and **nothing above this function ever sees an SDK exception**.

**The request parameters, each earning its place:**
- `model="nova-3"` — Deepgram's current best.
- `smart_format=True` — punctuation, numbers, currency; makes transcripts
  readable and implies punctuation for utterance splitting.
- `diarize=True` + `diarize_model="v2"` — who spoke; the current docs
  recommend pinning the diarizer version explicitly (v2 is the latest batch
  diarizer).
- `utterances=True` — Deepgram groups words into speaker-turn segments with
  start/end times: *exactly* our `{speaker, start, end, text}` contract, no
  reconstruction from word lists.
- `detect_language=True` — one dominant BCP-47 tag per channel
  (`detected_language`), which fills our `language` field.

**Why the SDK must not retry.** arq owns retries (M2 scope note). If the SDK
*also* retried internally, a flaky provider would trigger `SDK_retries ×
arq_tries` total attempts — multiplied load exactly when the provider is
struggling. v7 retries only if asked (`request_options={"max_retries": ...}`);
we don't ask, and we say why in a comment.

**Timeout discipline.** A 30-minute file transcribes in well under two minutes
on nova-3, but the request *can* hang. `timeout_in_seconds` (default 600 via
config) turns "hangs forever" into a classified retryable `provider_timeout`.

**Defensive mapping.** The response is mapped in one `_map_response` function.
A 200 whose shape we can't navigate raises `provider_response_invalid`
(permanent) instead of an `AttributeError` crash landing in `internal_error` —
same outcome for the row, but a diagnosable code. An empty transcript
(silence, hold music) becomes `no_speech_detected`: "completed with nothing"
would poison M3 analysis downstream.

**The fault-injection hook.** A dev-only env check at the top of
`transcribe()` lets 2.3/2.6 force either failure class without abusing real
credentials or waiting for a real 429. It reads `os.environ` at call time (not
Settings) — it's a test lever, not configuration, and unsetting it requires no
code path in prod.

**Why verification needs R2.** The worker presigns whatever endpoint it knows
— locally that's `http://minio:9000/...`, an address that exists only on the
docker network. Deepgram fetches the URL *from its cloud*: it must be publicly
reachable. Pointing the local `.env` at R2 is a config-only switch — which is
precisely what the M1 storage seam was built to prove.

### Steps

1. Add the SDK (from `backend/`):

   ```bash
   uv add deepgram-sdk
   ```

2. Extend `backend/app/core/config.py` — add to `Settings`:

   ```python
       # Deepgram
       deepgram_api_key: str = ""
       deepgram_request_timeout: int = 600   # seconds; classified provider_timeout beyond
       deepgram_max_concurrency: int = 5     # PAYG quota is 50; deliberate headroom (2.4)

       # LLM concurrency placeholder — used by M3, created alongside in 2.4
       llm_max_concurrency: int = 5

       # Storage
       storage_region: str = "auto"          # R2 wants "auto"; MinIO ignores it
   ```

   (`deepgram_api_key` already exists — keep it; add the rest.)

3. Pass the region in `backend/app/services/storage.py` — R2 presigning needs
   it; MinIO ignores it:

   ```python
       def _client(self):
           return self._session.client(
               "s3",
               endpoint_url=settings.storage_endpoint_url,
               aws_access_key_id=settings.storage_access_key,
               aws_secret_access_key=settings.storage_secret_key,
               region_name=settings.storage_region,
           )
   ```

4. Point the local `.env` at R2 (values from the M0 credential setup) and
   mirror the keys in `.env.example` with placeholder values:

   ```bash
   STORAGE_ENDPOINT_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
   STORAGE_ACCESS_KEY=<r2-access-key-id>
   STORAGE_SECRET_KEY=<r2-secret-access-key>
   STORAGE_BUCKET_NAME=<r2-bucket>
   DEEPGRAM_API_KEY=<deepgram-key>
   ```

   To work offline on stub-only tasks, flip these back to the MinIO values.

5. Replace the body of `backend/app/services/transcription.py`:

   ```python
   """Transcription service — real Deepgram integration (M2).

   Same signature as the M1 stub: presigned URL in, canonical transcript
   dict out. All SDK/network failures are translated into the errors.py
   taxonomy at this boundary — callers never see an SDK exception.
   """

   import logging
   import os

   import httpx
   from deepgram import AsyncDeepgramClient
   from deepgram.core.api_error import ApiError

   from app.core.config import settings
   from app.services.errors import (
       PermanentProviderError,
       RetryableProviderError,
       classify_http_status,
   )

   logger = logging.getLogger(__name__)

   # One client per process — it's a thin config wrapper around an httpx pool.
   _client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)


   def _inject_fault() -> None:
       """Dev-only failure lever for verifying retry paths (tasks 2.3/2.6).

       Reads the env var at call time so toggling it needs only a container
       restart, never a code change. Unset in production.
       """
       fault = os.environ.get("FAULT_INJECT_TRANSCRIPTION", "")
       if fault == "retryable":
           raise RetryableProviderError("provider_unavailable", "injected fault")
       if fault == "permanent":
           raise PermanentProviderError("audio_unreadable", "injected fault")


   async def transcribe(audio_url: str) -> dict:
       """Transcribe the audio at `audio_url` with speaker diarization.

       Returns: {language, text, duration, utterances: [{speaker, start, end, text}]}
       Raises: RetryableProviderError | PermanentProviderError
       """
       _inject_fault()
       try:
           response = await _client.listen.v1.media.transcribe_url(
               url=audio_url,
               model="nova-3",
               smart_format=True,
               diarize=True,
               diarize_model="v2",      # pin the current batch diarizer
               utterances=True,
               detect_language=True,
               # arq owns retries — SDK-internal retries would multiply
               # load on a struggling provider (tries x retries).
               request_options={"timeout_in_seconds": settings.deepgram_request_timeout},
           )
       except ApiError as e:
           raise classify_http_status(
               e.status_code,
               permanent_code="audio_unreadable",
               detail=str(e.body)[:500],
           ) from e
       except httpx.TimeoutException as e:
           raise RetryableProviderError("provider_timeout", repr(e)) from e
       except httpx.HTTPError as e:
           # Connection-level failures (DNS, reset) — no status to classify.
           raise RetryableProviderError("provider_unavailable", repr(e)) from e

       return _map_response(response)


   def _map_response(response) -> dict:
       """Map Deepgram's response into the M1 transcript contract."""
       try:
           results = response.results
           channel = results.channels[0]
           alternative = channel.alternatives[0]
           transcript = {
               "language": getattr(channel, "detected_language", None),
               "text": alternative.transcript,
               "duration": response.metadata.duration,
               "utterances": [
                   {"speaker": u.speaker, "start": u.start,
                    "end": u.end, "text": u.transcript}
                   for u in (results.utterances or [])
               ],
           }
       except (AttributeError, IndexError, TypeError) as e:
           # A 200 whose shape we can't navigate: diagnosable, not a crash.
           raise PermanentProviderError("provider_response_invalid", repr(e)) from e

       if not transcript["text"].strip():
           # Silence / hold music: "completed with nothing" would poison
           # M3 analysis — fail visibly instead.
           raise PermanentProviderError("no_speech_detected", "empty transcript")
       return transcript
   ```

6. Create `backend/scripts/check_deepgram.py` — one real end-to-end
   service-level check before wiring the worker:

   ```python
   """Smoke-test the real Deepgram integration: upload a fixture to storage,
   presign, transcribe, print the mapped result.

   Requires .env pointed at R2 + a real DEEPGRAM_API_KEY.
   Run: cd backend && uv run python scripts/check_deepgram.py <path-to-audio>
   """

   import asyncio
   import json
   import sys
   from pathlib import Path

   sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

   from app.services.storage import storage
   from app.services.transcription import transcribe


   async def main(path: str) -> None:
       key = f"smoke-{Path(path).name}"
       with open(path, "rb") as f:
           await storage.save(f, key)
       url = await storage.presigned_url(key)
       result = await transcribe(url)
       await storage.delete(key)

       print(f"language={result['language']} duration={result['duration']:.1f}s "
             f"utterances={len(result['utterances'])}")
       for u in result["utterances"][:5]:
           print(f"  [spk {u['speaker']} {u['start']:6.1f}-{u['end']:6.1f}] {u['text'][:70]}")
       assert result["utterances"], "expected at least one utterance"
       assert {"speaker", "start", "end", "text"} <= result["utterances"][0].keys()
       print("shape OK — full result below")
       print(json.dumps(result, indent=2)[:2000])


   if __name__ == "__main__":
       asyncio.run(main(sys.argv[1]))
   ```

   Run it against a *short real* recording first (cheap), then spot-check a
   speaker-labeled line against the audio:

   ```bash
   cd backend && uv run python scripts/check_deepgram.py tests/fixtures/audio/<short-real-file>.mp3
   ```

   If a field access dies with `provider_response_invalid`, print the raw
   `response` in a REPL and adjust `_map_response` — the v7 typed models are
   the source of truth, this doc is written against their documented shape.

7. Commit.

### Files created / changed
- `backend/scripts/check_deepgram.py`
- edits: `backend/app/services/transcription.py`, `backend/app/core/config.py`,
  `backend/app/services/storage.py`, `.env`, `.env.example`, `backend/pyproject.toml`

---

## Task 2.3 — Retry & backoff in the worker

### Explain first

**How arq retries actually work.** An arq task that raises a normal exception
is *done* — arq records the failure, nothing re-runs. Re-running happens only
when the task raises `arq.Retry(defer=seconds)`: arq re-enqueues the same job
to run after the deferral. `ctx["job_try"]` tells the task which attempt it's
on (starting at 1), and `WorkerSettings.max_tries` is the hard ceiling. So
retry policy lives *in the task*, exactly where the error classification is.

**The three-way except.** Classification (2.1) maps every failure to a branch:
- `PermanentProviderError` → mark `failed` + its code, re-raise (arq logs it).
- `RetryableProviderError`, tries remain → raise `Retry(defer=5·5^(try−1))`
  — 5s, 25s, 125s. **The row is not touched**: it stays `transcribing`
  through the wait, which is the truth ("this call is still being worked
  on"). A `retrying` status would grow the state machine for cosmetics.
- `RetryableProviderError`, last try → *now* it's permanent in effect: mark
  `failed` with the retryable code, re-raise. `failed` + a retryable-class
  code is how the UI reads "gave up after backoff" — no extra code needed.
- Anything else (`except Exception`) → `internal_error`, unchanged from M1:
  bugs are permanent until understood.

**Why exponential backoff.** A 429 means "you're sending too fast" — retrying
in 1s makes it worse. 5×5^n spaces attempts out by ~2.5 minutes total, long
enough for a rate window to reset or a blip to pass, short enough that a call
isn't stuck for an hour.

**Re-entry and the self-transition guard.** On an arq re-run the row is
already `transcribing`, and `transcribing → transcribing` is illegal in the
1.1 table. Rather than polluting the *rulebook* with self-loops, `_advance`
gets a no-op guard: already in the target state → nothing to assert, nothing
to write. The table keeps describing real movements only. The same guard is
what lets the 2.5 retry endpoint pre-set `transcribing` before the worker
picks the job up.

**`InvalidTransition` stays sacred.** If a stale or duplicate job finds the
row in a state it can't legally leave, the task re-raises *without writes* —
marking it `failed` would trample a healthy row that some other job owns.

**`job_timeout` arithmetic.** The ceiling must cover the slowest honest run:
one Deepgram request (≤ 600s by our timeout) + analysis + DB work. 900s. Note
`Retry` deferral does *not* count against `job_timeout` — the job isn't
running while it waits.

### Steps

1. Rewrite `backend/app/worker/tasks.py`:

   ```python
   """The pipeline task: walks a call through the state machine with
   DB-checkpointed stages. Stage work is skipped when its output already
   exists (idempotent re-runs). Failures are classified: retryable errors
   back off via arq's Retry; permanent errors mark the row failed with a
   machine-readable error_code."""

   import logging
   import uuid

   from arq import Retry
   from sqlalchemy.ext.asyncio import AsyncSession

   from app.core.db import SessionFactory
   from app.models.call import Call
   from app.models.states import CallStatus, InvalidTransition, assert_transition
   from app.services.analysis import analyze
   from app.services.errors import PermanentProviderError, RetryableProviderError
   from app.services.storage import storage
   from app.services.transcription import transcribe

   logger = logging.getLogger(__name__)

   MAX_TRIES = 3
   BACKOFF_BASE_SECONDS = 5


   def _backoff(job_try: int) -> int:
       """5s, 25s, 125s — exponential, ~2.5 min total before giving up."""
       return BACKOFF_BASE_SECONDS * 5 ** (job_try - 1)


   async def _advance(session: AsyncSession, call: Call, to_status: CallStatus) -> None:
       """Move the call to `to_status` — legality checked, then checkpointed.

       No-ops if already there: an arq re-run (or a retry endpoint that
       pre-set the status) passes through without self-transition noise.
       """
       if CallStatus(call.status) == to_status:
           return
       assert_transition(CallStatus(call.status), to_status)
       call.status = to_status
       await session.commit()


   async def _fail(session: AsyncSession, call: Call, error_code: str) -> None:
       """Land the row in `failed` with a diagnosable code — never silently."""
       await session.rollback()
       await session.refresh(call)
       call.status = CallStatus.FAILED
       call.error_code = error_code
       await session.commit()


   async def process_call(ctx: dict, call_id: str) -> None:
       job_try = ctx.get("job_try", 1)
       async with SessionFactory() as session:
           call = await session.get(Call, uuid.UUID(call_id))
           if call is None:
               logger.error("process_call: no such call %s", call_id)
               return

           try:
               # Stage 1 — transcription (skipped if checkpoint exists).
               await _advance(session, call, CallStatus.TRANSCRIBING)
               if call.transcript is None:
                   audio_url = await storage.presigned_url(call.storage_key)
                   call.transcript = await transcribe(audio_url)
                   await session.commit()  # checkpoint: transcript is durable
               else:
                   logger.info("call %s: transcript checkpoint hit — skipping Deepgram", call_id)

               # Stage 2 — analysis (skipped if checkpoint exists).
               await _advance(session, call, CallStatus.ANALYZING)
               if call.analysis is None:
                   call.analysis = await analyze(call.transcript["text"])
                   await session.commit()  # checkpoint: analysis is durable
               else:
                   logger.info("call %s: analysis checkpoint hit — skipping", call_id)

               await _advance(session, call, CallStatus.COMPLETED)
               logger.info("call %s completed (try %d)", call_id, job_try)

           except InvalidTransition:
               # Stale/duplicate job found the row in a state it can't leave.
               # Leave the row exactly as found — another job owns it.
               logger.warning("call %s: illegal transition on try %d — aborting without writes",
                              call_id, job_try)
               raise
           except PermanentProviderError as e:
               await _fail(session, call, e.error_code)
               raise
           except RetryableProviderError as e:
               if job_try < MAX_TRIES:
                   delay = _backoff(job_try)
                   logger.warning("call %s: %s (try %d/%d) — retrying in %ds",
                                  call_id, e.error_code, job_try, MAX_TRIES, delay)
                   # Row untouched: stays `transcribing` through the wait.
                   raise Retry(defer=delay) from e
               # Tries exhausted: permanent in effect. `failed` + a
               # retryable-class code reads as "gave up after backoff".
               await _fail(session, call, e.error_code)
               raise
           except Exception:
               await _fail(session, call, "internal_error")
               raise
   ```

2. Update `backend/app/worker/settings.py`:

   ```python
   """arq worker settings."""

   from arq.connections import RedisSettings

   from app.core.config import settings
   from app.worker.tasks import MAX_TRIES, process_call


   class WorkerSettings:
       redis_settings = RedisSettings.from_dsn(settings.redis_url)
       functions = [process_call]
       max_tries = MAX_TRIES   # one source of truth — the task's retry policy
       job_timeout = 900       # ceiling: 600s Deepgram timeout + analysis + slack
   ```

3. Forward the fault-injection env vars to the worker in
   `docker-compose.yml` (worker service, `environment:` block):

   ```yaml
   FAULT_INJECT_TRANSCRIPTION: ${FAULT_INJECT_TRANSCRIPTION:-}
   FAULT_INJECT_ANALYSIS: ${FAULT_INJECT_ANALYSIS:-}
   ```

4. Verify the retryable path (no real API calls — injection fires before the
   SDK):

   ```bash
   FAULT_INJECT_TRANSCRIPTION=retryable docker compose up -d --build
   curl -s -X POST localhost:8000/api/calls \
     -F "file=@tests/fixtures/audio/stubs/stub_0001.wav" > /dev/null
   docker compose logs -f worker
   # expect: "provider_unavailable (try 1/3) — retrying in 5s"
   #         "provider_unavailable (try 2/3) — retrying in 25s"
   #         then failed; row shows error_code=provider_unavailable
   docker compose exec postgres psql -U postgres -d call_analyzer \
     -c "select status, error_code from calls order by created_at desc limit 1"
   # → failed | provider_unavailable
   ```

   During the deferral windows, confirm the row reads `transcribing` (the
   documented "still being worked on" truth).

5. Verify the permanent path — no retries, straight to failed:

   ```bash
   FAULT_INJECT_TRANSCRIPTION=permanent docker compose up -d
   # upload another stub; logs show ONE attempt, no "retrying in"
   # row → failed | audio_unreadable
   ```

6. Unset the fault (`docker compose up -d` with the var absent) and commit.

### Files changed
- `backend/app/worker/tasks.py`, `backend/app/worker/settings.py`,
  `docker-compose.yml`

---

## Task 2.4 — Per-provider concurrency semaphore

### Explain first

**What it protects.** Deepgram's pay-as-you-go tier allows **50 concurrent**
pre-recorded requests, shared by everything using the key. arq's `max_jobs`
caps *jobs* per worker — but a job is mostly non-Deepgram time (DB writes,
analysis, waiting), so `max_jobs` is the wrong knob for provider pressure.
The semaphore caps *simultaneous in-flight Deepgram requests* specifically.

**How an `asyncio.Semaphore` works.** It holds N permits; `async with sem:`
takes one (or waits until one frees) and releases on exit — even on
exception. With N=5 and 10 running jobs, at most 5 are inside a Deepgram
request; the rest queue up at the `async with` line, costing nothing but a
suspended coroutine.

**Why it lives in `ctx`, and the task holds it.** arq's `on_startup` hook
runs once per worker process and populates `ctx`, which every job invocation
receives — the natural home for per-process shared objects. The *task* wraps
the call (`async with ctx["deepgram_sem"]: transcribe(...)`) so
`transcription.py` stays free of global state and ignorant of worker
machinery — the same seam discipline as everywhere else.

**The knob layering (plan §4), now complete:** queue absorbs bursts
(unbounded) → `max_jobs` caps work per worker (arq default 10) → semaphore
caps provider pressure (5). Each turns independently. Default 5, not 50: the
key may be shared (dev + deployed instance), and nothing at demo volume needs
more. Honest production note for the README: with N workers the effective cap
is N×5 — the real fix at scale is Deepgram callbacks, not a distributed lock.

**Hold-time.** A permit is held for the whole request — up to minutes for a
30-minute file. That's correct: the permit *is* the in-flight request. Up to
`max_jobs − 5` jobs may park at the semaphore holding only their max_jobs
slot; at demo volume that's fine and documented.

**The `llm_sem` placeholder** is created now (one line) because M3's analysis
call has the same shape of quota (Gemini free tier), and creating both here
makes the pattern legible in one place.

### Steps

1. Update `backend/app/worker/settings.py` — add the hook:

   ```python
   """arq worker settings."""

   import asyncio

   from arq.connections import RedisSettings

   from app.core.config import settings
   from app.worker.tasks import MAX_TRIES, process_call


   async def on_startup(ctx: dict) -> None:
       """Per-process shared objects, one per provider quota (plan §4:
       'per-provider semaphores cap concurrent external calls')."""
       ctx["deepgram_sem"] = asyncio.Semaphore(settings.deepgram_max_concurrency)
       ctx["llm_sem"] = asyncio.Semaphore(settings.llm_max_concurrency)  # used by M3


   class WorkerSettings:
       redis_settings = RedisSettings.from_dsn(settings.redis_url)
       functions = [process_call]
       on_startup = on_startup
       max_tries = MAX_TRIES
       job_timeout = 900
   ```

2. Wrap the call in `backend/app/worker/tasks.py` (stage 1 only changes):

   ```python
               if call.transcript is None:
                   audio_url = await storage.presigned_url(call.storage_key)
                   async with ctx["deepgram_sem"]:
                       logger.info("call %s: deepgram slot acquired", call_id)
                       call.transcript = await transcribe(audio_url)
                   await session.commit()  # checkpoint: transcript is durable
   ```

3. Verify serialization without touching Deepgram — injection fires *inside*
   the semaphore-guarded region only if we look at logs; instead observe with
   the stub delay trick: set `DEEPGRAM_MAX_CONCURRENCY=1`, re-point `.env` at
   MinIO, and temporarily let faults simulate slow calls — simplest honest
   check is timestamps:

   ```bash
   DEEPGRAM_MAX_CONCURRENCY=1 FAULT_INJECT_TRANSCRIPTION= docker compose up -d
   # upload 3 stubs back-to-back, then:
   docker compose logs worker | grep "deepgram slot acquired"
   # with concurrency 1 the three acquisition timestamps are strictly
   # serialized (each after the previous call finished); with the default 5
   # they cluster together. (Requires the real path or the M1 stub delay —
   # either way, the *ordering* of the log lines is the evidence.)
   ```

   Then restore `DEEPGRAM_MAX_CONCURRENCY` to default and the `.env` to R2.

4. Commit.

### Files changed
- `backend/app/worker/settings.py`, `backend/app/worker/tasks.py`

---

## Task 2.5 — Retry endpoint

### Explain first

**What "retry" means here.** arq's automatic retries (2.3) are *machine*
retries of a still-live job. This endpoint is the *human* retry: a call
already landed in `failed`, a person decides to try again. Per the M2 scope
note it's a **fresh enqueue** — the exhausted arq job is history; the state
machine and the checkpoints carry all the context a re-run needs. If the
transcript survived (LLM failed, transcription didn't), the checkpoint makes
the retry skip Deepgram entirely — the "never double-pay" contract.

**Why the endpoint does the transition.** Two candidates could move
`failed → transcribing`: the endpoint (now) or the worker (on pickup). The
endpoint doing it wins twice: the UI reflects the retry *instantly* (no
"failed" lingering while queued), and it makes double-submits structurally
impossible — the second click finds the row in `transcribing`, which is not
`failed`, and gets `409`. The transition check *is* the dedupe guard; no
deterministic job ids, no locks. The cost is honest and small: a queued-but-
not-started call reads `transcribing` a bit early.

**Race with an in-flight job: closed by the state machine.** Only `failed`
rows pass the precondition, and a `failed` row *has no live job* — the job
that marked it failed finished doing so. During an arq backoff deferral the
row reads `transcribing` (2.3 kept it untouched), so the endpoint refuses it.
Every path is covered by reading one column.

**`409 Conflict`** is the precise status: the request is well-formed, the
resource exists, but its current state forbids the operation. The body says
what state it *is* in, so a UI can render "already retrying" vs "completed".

**Failure symmetry with upload.** If enqueue fails after the transition, the
row would claim `transcribing` with no job to ever advance it — a silent
zombie. Same medicine as 1.5: revert to `failed` with `enqueue_failed`,
visible and re-retryable.

### Steps

1. Add a shared accepted-response model to `backend/app/models/schemas.py`:

   ```python
   class CallAccepted(BaseModel):
       """202 body for both upload and retry — the id to poll and the
       status the row was left in."""
       id: uuid.UUID
       status: CallStatus
   ```

2. Add to `backend/app/api/calls.py` (imports: `assert_transition` from
   `app.models.states`, `CallAccepted` from schemas):

   ```python
   @router.post("/{call_id}/retry", status_code=202, response_model=CallAccepted)
   async def retry_call(
       call_id: uuid.UUID,
       session: AsyncSession = Depends(get_session),
       arq_pool: ArqRedis = Depends(get_arq_pool),
   ) -> CallAccepted:
       call = await session.get(Call, call_id)
       if call is None:
           raise HTTPException(404, detail="call not found")
       if call.status != CallStatus.FAILED:
           # Doubles as the double-submit guard: a second click finds the
           # row already in `transcribing` and lands here.
           raise HTTPException(
               409, detail=f"call is '{call.status}', only failed calls can be retried"
           )

       # The endpoint owns the transition (instant UI truth + dedupe);
       # the worker's _advance no-ops when it finds the status pre-set.
       assert_transition(CallStatus(call.status), CallStatus.TRANSCRIBING)
       call.status = CallStatus.TRANSCRIBING
       call.error_code = None
       await session.commit()

       try:
           await arq_pool.enqueue_job("process_call", str(call_id))
       except Exception:
           # Same medicine as upload: a row claiming progress with no job
           # behind it is a silent zombie — make the failure visible.
           call.status = CallStatus.FAILED
           call.error_code = "enqueue_failed"
           await session.commit()
           raise HTTPException(500, detail="retry accepted but queueing failed")

       return CallAccepted(id=call.id, status=CallStatus(call.status))
   ```

   Optionally switch the upload handler's return to `CallAccepted` for
   symmetry (`response_model=CallAccepted`, return
   `CallAccepted(id=call_id, status=CallStatus(call.status))`).

3. Verify:

   ```bash
   # make a failed call (fault-inject permanent), then clear the fault:
   FAULT_INJECT_TRANSCRIPTION=permanent docker compose up -d
   ID=$(curl -s -X POST localhost:8000/api/calls \
     -F "file=@tests/fixtures/audio/stubs/stub_0001.wav" | jq -r .id)
   sleep 3   # let it fail
   docker compose up -d   # fault cleared (var unset)

   curl -s -X POST localhost:8000/api/calls/$ID/retry | jq
   # → 202 {"id": ..., "status": "transcribing"}, error_code cleared

   curl -s -w "%{http_code}\n" -X POST localhost:8000/api/calls/$ID/retry
   # → 409 (already transcribing — the dedupe guard)

   curl -s -w "%{http_code}\n" -X POST \
     localhost:8000/api/calls/00000000-0000-0000-0000-000000000000/retry
   # → 404

   # retrying a completed call → 409 with "call is 'completed'..."
   ```

4. Commit.

### Files changed
- `backend/app/api/calls.py`, `backend/app/models/schemas.py`

---

## Task 2.6 — End-to-end verification

### Explain first

**Why this is a task, not a footnote.** M2's expected result is phrased
entirely as verifications: long file works, failures land in the right path,
retry provably skips Deepgram. Each claim gets a demonstrated, recorded
answer — and the record (`.claude/milestone_2_results.md`) becomes raw
material for the README's testing-strategy and architecture sections in M7.

**The checkpoint proof needs a failure *after* transcription.** Transcription
faults can't produce a row that has a transcript *and* failed. So the
analysis stub gets the same one-line fault hook — which M3 keeps, since the
real LLM call will want the same lever. The proof then has two independent
witnesses: the worker log line ("transcript checkpoint hit — skipping
Deepgram") and the Deepgram console's request count, which must not move on
the retry.

**Honest failure coverage.** The permanent path is tested against the *real*
provider twice — garbage bytes with a `.wav` name (Deepgram 400 →
`audio_unreadable`) and a silent stub file (200, empty transcript →
`no_speech_detected`, our own mapping rule). The retryable path was already
verified via injection in 2.3; a real 429 can't be summoned on demand and
doesn't need to be.

### Steps

1. Add the fault hook to `backend/app/services/analysis.py` (mirrors 2.2):

   ```python
   import os

   from app.services.errors import PermanentProviderError

   # inside analyze(), first line:
       if os.environ.get("FAULT_INJECT_ANALYSIS") == "permanent":
           raise PermanentProviderError("internal_error", "injected fault")
   ```

2. Create `.claude/milestone_2_results.md` and fill it in as each check runs
   (timings, observed states, error codes, Deepgram console counts).

3. **Check A — short real call, full pipeline.** `.env` on R2, stack up.
   Upload a short real recording via the API; poll to `completed`; fetch the
   detail endpoint and confirm `language`, `duration`, speaker-labeled
   utterances with sane timestamps.

4. **Check B — the long recording (the defining constraint).** Upload the
   20–30 minute file. Record: upload latency (must be ~instant — streaming),
   time in `transcribing`, utterance count, speakers found. Confirm memory
   didn't spike (`docker stats` during the run — the file never transits the
   worker).

5. **Check C — retryable path** (already proven in 2.3; re-run briefly and
   record the log excerpt showing 5s/25s deferrals and final
   `provider_unavailable`).

6. **Check D — permanent path, real provider.**

   ```bash
   head -c 100000 /dev/urandom > /tmp/garbage.wav
   curl -s -X POST localhost:8000/api/calls -F "file=@/tmp/garbage.wav"
   # → row lands failed | audio_unreadable (Deepgram 4xx), ONE attempt in logs
   ```

   Then upload one *silent* stub WAV → `failed | no_speech_detected` (our
   empty-transcript rule, exercised against the real provider).

7. **Check E — the checkpoint proof (never double-pay Deepgram).**

   ```bash
   # fail AFTER transcription:
   FAULT_INJECT_ANALYSIS=permanent docker compose up -d
   ID=$(curl -s -X POST localhost:8000/api/calls -F "file=@<short-real-file>" | jq -r .id)
   # poll → failed; detail endpoint shows transcript PRESENT, analysis null
   # note the request count in the Deepgram console now

   docker compose up -d          # clear the fault
   curl -s -X POST localhost:8000/api/calls/$ID/retry
   # poll → completed
   docker compose logs worker | grep "checkpoint hit"
   # → "transcript checkpoint hit — skipping Deepgram"
   # Deepgram console request count: UNCHANGED. Two witnesses, proof done.
   ```

8. Write the summary paragraph in `milestone_2_results.md` (what was
   verified, with numbers) and commit.

### Files created / changed
- `.claude/milestone_2_results.md`
- edits: `backend/app/services/analysis.py`

---

## Milestone exit check

The union of 2.6's checks A–E: long real call → speaker-labeled transcript in
the DB; injected retryable failure → observed backoff → `failed` with a
retryable-class code; garbage upload → `failed | audio_unreadable`; silence →
`failed | no_speech_detected`; retry of a transcript-bearing failed call →
`completed` with Deepgram provably untouched.

## Execution order

```
2.1 errors → 2.2 deepgram → 2.3 retry/backoff → 2.4 semaphore → 2.5 retry endpoint → 2.6 verification
```

Each task ends with a commit.
