# Milestone 2 — Verification Results

Raw notes from the reduced-M2 end-to-end checks (real R2 + real Deepgram, via
the default docker-compose stack). Source material for the M7 README.

## Environment
- Storage: Cloudflare R2 (bucket `call`), presigned URLs fetchable from the
  worker container.
- STT: Deepgram `nova-3`, `diarize_model=v2`, `utterances`, `smart_format`,
  `detect_language`, via `deepgram-sdk` v7.6.0 (`AsyncDeepgramClient`).
- Worker: arq, `max_tries=1`, `job_timeout=900`.

## Check A — long recording, happy path (defining constraint)
- File: `call_4.mp3` (~20 min).
- Upload returned `202 {status: uploaded}` immediately (streamed to R2).
- Pipeline walked `uploaded → transcribing → analyzing → completed`.
- Detail endpoint after completion:
  - `status=completed`, `error_code=null`
  - `language=en`, `duration=1209.18s`, `utterances=60`, two speakers (0/1).
- Transcript persisted in the `calls.transcript` JSONB in the contract shape.

## Check B — permanent failure, real provider rejection
- File: 100 KB of `/dev/urandom` named `garbage.wav`.
- Deepgram returned 4xx → classified `audio_unreadable`.
- Result: `status=failed`, `error_code=audio_unreadable`, **one attempt**.
- Worker log: `call <id> failed: audio_unreadable`.

## Check C — silence path (from 2.2 service-level check)
- A silent stub WAV → Deepgram 200 with empty transcript → mapped to
  `no_speech_detected` (permanent). Confirms the empty-transcript rule against
  the real provider. (The 1000 stub WAVs are all silence — real-transcription
  checks must use `recordings/`.)

## Not covered (deferred to future improvements)
- Automatic retry-with-backoff, the retry endpoint, and the concurrency
  semaphore are out of reduced-M2 scope. A transient provider failure marks the
  call `failed` (visible, with a retryable-class code); recovery is re-upload.

## Notable finding
- `diarize_model` cannot be combined with `diarize` (Deepgram 400) — the docs
  imply `diarize_model` enables diarization by itself. Caught against the live
  API; fixed to send `diarize_model=v2` alone.
