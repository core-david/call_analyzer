# Milestone 3 — Task Implementation Guide

> Companion to `milestone_3.md`. Each task: concepts explained first (this doc
> doubles as a learning reference), then the implementation with code, then a
> manual verify step. No pytest in M3 — tests are M5.
>
> **Decisions locked in before writing this doc:**
> - SDK/model: `google-genai` v2.14.0 (`from google import genai`) +
>   `gemini-2.5-flash`, async via `client.aio.models.generate_content`.
> - Structured output: pass the Pydantic `AnalysisResult` as `response_schema`
>   with `response_mime_type="application/json"` — Gemini returns JSON conforming
>   to the schema; we validate it once with the same model.
> - `analyze()` takes the **diarized transcript dict** (not flat text); the
>   worker passes `call.transcript`. The service renders `Speaker N: …` lines.
> - The LLM infers agent/customer roles from content; `mood` is keyed by role.
> - `summary` always in English; enums are language-neutral.
> - `temperature=0` for reproducible tags.
> - Failures reuse the M2 taxonomy: Gemini transport error → `classify_http_status`
>   (permanent code `analysis_failed`); empty/blocked response → `analysis_blocked`;
>   post-response validation failure → `analysis_invalid`. All permanent;
>   `max_tries=1`, no retry.
> - Deferred (future): repair-retry, edge-case hardening, tagging-eval methodology.

---

## Task 3.1 — Analysis schema & closed vocabularies

### Explain first

**The schema is the contract, three times over.** One set of Pydantic models
does three jobs: it's handed to Gemini as `response_schema` (so the model emits
exactly these fields and enum values), it validates whatever comes back, and it
documents the shape stored in the `calls.analysis` JSONB and served by the
detail endpoint. Define it once, in one place, and every layer agrees by
construction.

**Why `StrEnum` for the vocabularies.** Same reasoning as `CallStatus`: a
`StrEnum` member *is* its string, so the one enum definition is simultaneously
the closed vocabulary the model must choose from, the JSON value persisted, and
the Python value compared in code. The enums *are* the tagging schema — the
single source of truth for what the model may emit. Free-text tags would
fragment and defeat aggregation (plan §3); a closed enum makes "how many calls
had a `price` objection?" a clean query.

**Why closed vocabularies mirror sales ops.** `outcome` is one mandatory value
(every call ended *somehow*), `objections` is multi-label (a call can raise
several, or none), `lead_temperature` is ordinal (cold < warm < hot). That
structure mirrors how a sales team actually consumes calls, which is the point
of tagging at all.

**Gemini structured-output note.** Gemini's `response_schema` supports Pydantic
models with enums, lists, and nested models — which is exactly our shape
(`AnalysisResult` → `Tags` + `SpeakerMood`, with `list[Objection]`). The 3.1
verify step confirms the SDK accepts the model as a schema before 3.2 depends
on it.

### Steps

1. Create `backend/app/models/analysis_schema.py`:

   ```python
   """Analysis output schema + closed tagging vocabularies.

   One definition serves three roles: Gemini's response_schema (structured
   output), the post-response validator, and the documented shape of the
   calls.analysis JSONB. The enums ARE the vocabularies — the single source
   of truth for what the model may emit.
   """

   from enum import StrEnum

   from pydantic import BaseModel, Field


   class Outcome(StrEnum):
       MEETING_SCHEDULED = "meeting_scheduled"
       INFO_REQUESTED = "info_requested"
       NOT_INTERESTED = "not_interested"
       NOT_QUALIFIED = "not_qualified"
       CLOSED_WON = "closed_won"
       NO_CLEAR_OUTCOME = "no_clear_outcome"


   class Objection(StrEnum):
       PRICE = "price"
       TIMING = "timing"
       AUTHORITY = "authority"
       NEED = "need"
       TRUST = "trust"
       COMPETITOR = "competitor"


   class LeadTemperature(StrEnum):
       COLD = "cold"
       WARM = "warm"
       HOT = "hot"


   class Intent(StrEnum):
       READY_TO_BUY = "ready_to_buy"
       EVALUATING = "evaluating"
       GATHERING_INFO = "gathering_info"
       PRICE_SHOPPING = "price_shopping"
       NOT_INTERESTED = "not_interested"


   class Mood(StrEnum):
       POSITIVE = "positive"
       NEUTRAL = "neutral"
       NEGATIVE = "negative"
       FRUSTRATED = "frustrated"


   class Tags(BaseModel):
       outcome: Outcome
       objections: list[Objection] = Field(default_factory=list)
       lead_temperature: LeadTemperature


   class SpeakerMood(BaseModel):
       agent: Mood
       customer: Mood


   class AnalysisResult(BaseModel):
       summary: str
       tags: Tags
       intent: Intent
       mood: SpeakerMood
   ```

2. Verify — the model validates the stub shape, and Gemini accepts it as a
   schema:

   ```bash
   cd backend && uv run python -c "
   from app.models.analysis_schema import AnalysisResult
   # Validates the exact shape the M1 stub returned (contract unchanged).
   sample = {
       'summary': 'Cold outreach; prospect flagged price, asked for details.',
       'tags': {'outcome': 'info_requested', 'objections': ['price'], 'lead_temperature': 'warm'},
       'intent': 'evaluating',
       'mood': {'agent': 'positive', 'customer': 'neutral'},
   }
   a = AnalysisResult.model_validate(sample)
   assert a.tags.outcome.value == 'info_requested'
   # Bad enum value is rejected.
   try:
       AnalysisResult.model_validate({**sample, 'intent': 'bogus'})
       raise SystemExit('should have failed')
   except Exception as e:
       print('rejects bad enum OK')

   # Gemini SDK accepts the model as a response_schema.
   from google.genai import types
   cfg = types.GenerateContentConfig(response_mime_type='application/json', response_schema=AnalysisResult)
   print('response_schema accepted OK')
   "
   ```

3. Commit.

### Files created
- `backend/app/models/analysis_schema.py`

---

## Task 3.2 — Gemini integration

### Explain first

**The v2 `google-genai` surface.** One `genai.Client(api_key=...)` per process;
the async call is `await client.aio.models.generate_content(model, contents,
config)`. Structured output is configured in `GenerateContentConfig`:
`response_mime_type="application/json"` + `response_schema=AnalysisResult`.
API failures raise `google.genai.errors.APIError` carrying `.code` (the HTTP
status) and `.message` — which is all `classify_http_status` needs, so Gemini
failures fold into the *same* taxonomy as Deepgram's.

**Why we validate `response.text` ourselves.** The SDK also exposes
`response.parsed` (a pre-built model), but we validate the raw JSON
(`response.text`) with `AnalysisResult` ourselves. That makes *our* schema the
authority and gives a real `analysis_invalid` path if the model ever returns
JSON that doesn't fit — rather than trusting the SDK's internal parsing. An
empty `response.text` means the candidate was dropped (usually a safety filter)
→ `analysis_blocked`.

**The diarized transcript, rendered.** `analyze()` now takes the whole
transcript dict and renders its utterances into `Speaker 0: … / Speaker 1: …`
lines. That's what lets the model (a) infer which anonymous speaker is the agent
vs. the customer and (b) judge mood per speaker. The service reads
`transcript["utterances"]` but never touches the DB — the worker owns
persistence, the service owns the provider call (plan §4). If utterances are
somehow absent, it falls back to the flat `text`.

**The prompt is code, kept in the service.** A system instruction states the
analyst role, the agent/customer inference rule, "summary in English", and that
every judgment must be grounded in the transcript. The closed vocabularies don't
need to be spelled out in prose — `response_schema` already constrains the model
to the enum values. Keeping the prompt in the service makes it versionable and
testable.

**`temperature=0`.** Tagging is a classification task, not creative writing;
determinism means the same call yields the same tags, which is what makes tags
aggregatable and evaluable over time.

**Failure = fail, not retry (reduced scope).** Consistent with M2: a permanent
Gemini/validation failure marks the call `failed` with a code; there's no
repair-retry. Because the analysis stage runs *after* the transcript checkpoint,
a later re-run (when retry ships) would skip Deepgram and only re-attempt
analysis — the "never double-pay" property, already built into the worker.

### Steps

1. Replace the body of `backend/app/services/analysis.py`:

   ```python
   """LLM analysis service — real Gemini integration (M3).

   Takes the diarized transcript, renders it speaker-labeled into the prompt,
   and asks Gemini for structured output conforming to AnalysisResult via
   response_schema. Transport and validation failures are translated into the
   errors.py taxonomy — the worker never sees an SDK exception.
   """

   import logging
   import os

   from google import genai
   from google.genai import errors, types
   from pydantic import ValidationError

   from app.core.config import settings
   from app.models.analysis_schema import AnalysisResult
   from app.services.errors import (
       PermanentProviderError,
       RetryableProviderError,
       classify_http_status,
   )

   logger = logging.getLogger(__name__)

   MODEL = "gemini-2.5-flash"

   # One client per process — a thin config wrapper.
   _client = genai.Client(api_key=settings.google_api_key)

   SYSTEM_INSTRUCTION = (
       "You are a sales-call analyst. You are given a diarized transcript of a "
       "sales call; each line is labeled with an anonymous speaker number. Infer "
       "which speaker is the sales AGENT (represents the company, drives the "
       "call) and which is the CUSTOMER (the prospect), from the content.\n\n"
       "Produce a structured analysis:\n"
       "- summary: a faithful 2-4 sentence summary, ALWAYS IN ENGLISH regardless "
       "of the call's language.\n"
       "- tags.outcome: the single best-fitting call outcome.\n"
       "- tags.objections: every objection the customer actually raised (empty "
       "if none).\n"
       "- tags.lead_temperature: how likely this lead is to convert.\n"
       "- intent: the customer's primary intent.\n"
       "- mood.agent / mood.customer: each speaker's overall mood.\n\n"
       "Base every judgment strictly on the transcript; do not invent facts."
   )


   def _inject_fault() -> None:
       """Dev-only failure lever for verifying the failure path (task 3.3)."""
       fault = os.environ.get("FAULT_INJECT_ANALYSIS", "")
       if fault == "retryable":
           raise RetryableProviderError("provider_unavailable", "injected fault")
       if fault == "permanent":
           raise PermanentProviderError("analysis_failed", "injected fault")


   def _render_transcript(transcript: dict) -> str:
       """Speaker-labeled lines for the prompt, from the diarized utterances."""
       utterances = transcript.get("utterances") or []
       if not utterances:
           return transcript.get("text", "")  # fall back to flat text
       return "\n".join(
           f"Speaker {u['speaker']}: {u['text']}" for u in utterances
       )


   async def analyze(transcript: dict) -> dict:
       """Analyze a diarized transcript into the closed tagging schema.

       Returns: {summary, tags:{outcome, objections, lead_temperature},
                 intent, mood:{agent, customer}}
       Raises: RetryableProviderError | PermanentProviderError
       """
       _inject_fault()
       rendered = _render_transcript(transcript)
       try:
           response = await _client.aio.models.generate_content(
               model=MODEL,
               contents=rendered,
               config=types.GenerateContentConfig(
                   system_instruction=SYSTEM_INSTRUCTION,
                   response_mime_type="application/json",
                   response_schema=AnalysisResult,
                   temperature=0,
               ),
           )
       except errors.APIError as e:
           raise classify_http_status(
               e.code, permanent_code="analysis_failed",
               detail=str(e.message)[:500],
           ) from e

       raw = response.text
       if not raw:
           # Empty candidate — usually a safety filter dropped the response.
           raise PermanentProviderError("analysis_blocked", "empty model response")
       try:
           result = AnalysisResult.model_validate_json(raw)
       except ValidationError as e:
           # 200 with JSON that doesn't fit the schema — diagnosable, never stored.
           raise PermanentProviderError("analysis_invalid", str(e)[:500]) from e

       return result.model_dump(mode="json")
   ```

2. Update the worker call in `backend/app/worker/tasks.py` — pass the whole
   transcript, not just the text:

   ```python
           if call.analysis is None:
               call.analysis = await analyze(call.transcript)  # was call.transcript["text"]
               await session.commit()  # checkpoint: analysis is durable
   ```

3. Remove the now-obsolete `backend/scripts/check_stubs.py` — both services are
   real now (transcription since M2, analysis here), and the analysis shape is
   enforced by `AnalysisResult` and exercised by the script in step 4.

   ```bash
   git rm backend/scripts/check_stubs.py
   ```

4. Create `backend/scripts/check_gemini.py` — a service-level smoke test that
   runs `analyze()` on a saved transcript JSON (no Deepgram re-call needed;
   use one produced by `check_deepgram.py --save`):

   ```python
   """Smoke-test the real Gemini analysis on a saved transcript JSON.

   Requires a real GOOGLE_API_KEY in .env.
   Produce a transcript first:
       uv run python scripts/check_deepgram.py <audio> --save
   Then:
       uv run python scripts/check_gemini.py transcripts/<stem>_transcript.json
   """

   import asyncio
   import json
   import sys
   from pathlib import Path

   sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

   from app.models.analysis_schema import AnalysisResult
   from app.services.analysis import analyze


   async def main(transcript_path: str) -> None:
       transcript = json.loads(Path(transcript_path).read_text())
       result = await analyze(transcript)
       # Round-trips through the schema (raises if the shape drifted).
       AnalysisResult.model_validate(result)
       print(json.dumps(result, indent=2, ensure_ascii=False))


   if __name__ == "__main__":
       asyncio.run(main(sys.argv[1]))
   ```

   Run it against a real transcript and read the output critically:

   ```bash
   cd backend && uv run python scripts/check_gemini.py transcripts/call_4_transcript.json
   ```

5. Commit.

### Files created / changed
- `backend/app/services/analysis.py`, `backend/scripts/check_gemini.py`
- edits: `backend/app/worker/tasks.py`
- removed: `backend/scripts/check_stubs.py`
- dependency: `google-genai` (already added)

---

## Task 3.3 — Verification

### Explain first

**What must be proven:** a completed call carries a schema-valid `analysis`
whose judgments actually match the call, and a Gemini failure lands the call in
`failed` with the right code — never a half-written analysis. The service-level
`check_gemini.py` (3.2) proves the call and shape in isolation; this task proves
it through the *worker*, end to end, and records the outputs for the M7
tagging-schema writeup.

**Reading the analysis critically** is the real work here: `response_schema`
guarantees the *shape*, not the *judgment*. Confirm the summary is faithful and
in English, `objections` reflect objections actually voiced, and agent/customer
roles weren't swapped.

### Steps

1. Full pipeline, real call (docker + R2 + Gemini):

   ```bash
   docker compose up -d --build
   ID=$(curl -s -X POST localhost:8000/api/calls \
     -F "file=@backend/tests/fixtures/audio/recordings/call_4.mp3" \
     | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
   # poll to completed, then:
   curl -s "localhost:8000/api/calls/$ID" \
     | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin)['analysis'], indent=2))"
   ```

   Read the analysis against the transcript. Repeat across the four
   `recordings/` (they differ in content) and record the outputs.

2. Failure path — force a Gemini failure via the injection lever and confirm the
   code and that the transcript checkpoint survives:

   ```bash
   FAULT_INJECT_ANALYSIS=permanent docker compose up -d
   # upload a real recording; it transcribes, then analysis fails:
   #   status = failed, error_code = analysis_failed
   # detail endpoint shows transcript PRESENT, analysis null
   docker compose up -d   # clear the fault
   ```

3. Record the four analyses and the failure observation in
   `.claude/milestone_3_results.md` (raw material for the M7 README).

4. Commit.

### Files created
- `.claude/milestone_3_results.md`

---

## Milestone exit check

A completed call carries a schema-valid `analysis` — a faithful English summary,
closed-vocabulary `tags` (outcome, objections, lead_temperature), customer
`intent`, and per-speaker `mood`; a forced Gemini failure lands the call in
`failed` with a machine-readable code, transcript checkpoint intact, never a
half-written analysis.

## Execution order

```
3.1 analysis schema → 3.2 gemini integration → 3.3 verification
```

Each task ends with a commit.
