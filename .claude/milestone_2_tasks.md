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
>
> **Reduced-scope revision (agreed after 2.2):** 2.3 is simplified to plain
> error handling — provider failures are classified into an `error_code` and
> the call is marked `failed`, with `max_tries=1` and **no backoff/retry**.
> Deferred to future improvements (the error taxonomy makes each drop-in):
> automatic retry-with-backoff, the **retry endpoint** (was 2.5), and the
> dedicated **concurrency semaphore** (was 2.4 — `max_jobs` already caps a
> single worker under Deepgram's 50-request limit). Recovery from a transient
> failure is re-upload until those land — a documented limitation.

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
## Task 2.3 — Error handling in the worker (simplified)

### Explain first

**Reduced scope.** The full 2.3 wired arq `Retry` + exponential backoff. The
agreed reduction drops that: the worker now *classifies and surfaces* failures
rather than retrying them. On any `ProviderError` it marks the call `failed`
with that error's machine-readable `error_code`; an unclassified exception
falls back to `internal_error`. `max_tries=1` — no automatic re-runs. This is
honest because every failure is visible with a diagnosable code; the only cost
is that recovery from a transient blip is a re-upload until the deferred retry
features (2.4/2.5 + auto-backoff) land. The 2.1 taxonomy already separates
retryable from permanent, so adding retry later is a localized change here.

**The checkpoint stays.** The `if call.transcript is None` guard is kept even
though nothing re-runs in normal operation: arq can redeliver a job if a worker
dies mid-flight, and the guard stops that redelivery from re-paying Deepgram.
Idempotency is cheap insurance, already written.

**`job_timeout` must exceed the Deepgram request timeout.** We allow a Deepgram
call up to `deepgram_request_timeout` (600s). If `job_timeout` were also 600s a
slow transcription could trip the job ceiling first, so it is raised to 900s
(600 Deepgram + analysis + slack).

**`_fail` sequencing.** A stage may fail after a partial change; `_fail` does
`rollback → refresh → set failed + error_code → commit` so the row lands cleanly
in `failed` with no half-written state.

### Steps

1. Edit `backend/app/worker/tasks.py` — import the taxonomy, add `_fail`, and
   split the except:

   ```python
   from app.services.errors import ProviderError

   async def _fail(session: AsyncSession, call: Call, error_code: str) -> None:
       """Land the row in `failed` with a diagnosable code — never silently."""
       await session.rollback()
       await session.refresh(call)
       call.status = CallStatus.FAILED
       call.error_code = error_code
       await session.commit()

   # ... inside process_call, replacing the catch-all except:
       except ProviderError as e:
           # Classified external-service failure: persist its error_code.
           logger.warning("call %s failed: %s", call_id, e.error_code)
           await _fail(session, call, e.error_code)
           raise
       except Exception:
           # Unclassified bug — mark failed, then let arq log the traceback.
           await _fail(session, call, "internal_error")
           raise
   ```

2. Edit `backend/app/worker/settings.py` — drop the M0 `ping` remnant; set:

   ```python
   max_tries = 1        # no auto-retry in reduced M2; failures land in `failed`
   job_timeout = 900    # must exceed the 600s Deepgram request timeout + analysis
   ```

3. Verify — imports clean, then the permanent path through the worker:

   ```bash
   cd backend && uv run python -c "import app.worker.tasks, app.worker.settings; print('ok')"
   # end-to-end (docker): fault-inject a permanent failure and confirm the code
   FAULT_INJECT_TRANSCRIPTION=permanent docker compose up -d --build
   # upload any file; the row lands: failed | audio_unreadable  (ONE attempt)
   ```

4. Commit.

### Files changed
- `backend/app/worker/tasks.py`, `backend/app/worker/settings.py`

---

## Task 2.4 — Per-provider concurrency semaphore *(deferred → future improvements)*

**Not built in reduced M2.** At single-worker scale arq's `max_jobs` (default
10) already caps concurrent Deepgram calls under the pay-as-you-go 50-request
limit, so an explicit semaphore changes nothing until a *second* worker exists
(N×10 > 50). The future implementation is small and known: an
`asyncio.Semaphore(settings.deepgram_max_concurrency)` created in an arq
`on_startup` hook and held by the task around `await transcribe(...)` (services
stay free of global state). The `deepgram_max_concurrency` / `llm_max_concurrency`
config knobs already exist. Documented as the multi-worker scaling step in
ARCHITECTURE.md / plan §7.

---

## Task 2.5 — Retry endpoint *(deferred → future improvements)*

**Not built in reduced M2.** `POST /calls/{id}/retry` is a clean future add:
allowed only from `failed`, it moves `failed → transcribing` (a transition the
state machine already models), clears `error_code`, and enqueues a fresh job.
The checkpoint skip means a retry after a persisted transcript never re-calls
Deepgram. The precondition (non-`failed` → `409`) doubles as a double-submit
guard. Nothing about it needs to change in the worker — it is purely additive.

---

## Task 2.6 — End-to-end verification

### Explain first

**What must be proven** in reduced M2: (1) a real long recording runs to
`completed` with a diarized transcript persisted, and (2) permanent provider
failures land in `failed` with the correct `error_code`. Retry/backoff and the
retry endpoint are out of scope, so their checks are dropped. Results are
recorded in `.claude/milestone_2_results.md` as raw material for the M7 README.

**Real speech vs. the stubs.** The 1000 stub WAVs are silence — usable for the
M1 architecture path but they all fail `no_speech_detected` against real
Deepgram. Real-transcription checks must use the `recordings/` files.

### Steps

1. Create `.claude/milestone_2_results.md`; fill it in as each check runs.

2. **Check A — the long recording (defining constraint).** Docker stack on R2;
   upload a 20–30 min recording via the API; poll to `completed`; fetch the
   detail endpoint and confirm `language`, `duration`, and speaker-labeled
   utterances with sane timestamps. Record upload latency (must be ~instant —
   streaming) and time in `transcribing`.

3. **Check B — permanent path, real provider:**

   ```bash
   head -c 100000 /dev/urandom > /tmp/garbage.wav
   curl -s -X POST localhost:8000/api/calls -F "file=@/tmp/garbage.wav"
   # → failed | audio_unreadable (Deepgram 4xx), one attempt
   # then upload a silent stub WAV → failed | no_speech_detected
   ```

4. **Check C — worker failure path via injection:**
   `FAULT_INJECT_TRANSCRIPTION=permanent`, upload, confirm `failed |
   audio_unreadable` with a single attempt and a visible log line.

5. Write the summary in `milestone_2_results.md` (numbers included) and commit.

### Files created
- `.claude/milestone_2_results.md`

---

## Milestone exit check

Upload a 20–30 minute recording → speaker-labeled transcript lands in the DB and
the call reaches `completed`; a garbage upload → `failed | audio_unreadable`; a
silent file → `failed | no_speech_detected`. Failures are always visible with a
machine-readable code — never a silent death.

## Execution order

```
2.1 errors → 2.2 deepgram → 2.3 error handling → 2.6 verification
```

(2.4 concurrency semaphore and 2.5 retry endpoint are deferred to future
improvements.) Each task ends with a commit.
