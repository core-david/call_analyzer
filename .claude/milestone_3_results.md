# Milestone 3 — Verification Results

Notes from the M3 end-to-end checks (real R2 + real Deepgram + real Gemini, via
the default docker-compose stack). Source material for the M7 README /
tagging-schema writeup.

## Environment
- LLM: Gemini `gemini-3.5-flash` via `google-genai` v2.14.0
  (`client.aio.models.generate_content`), `response_schema=AnalysisResult`,
  `temperature=0`.
- Analysis schema v2: `reasoning` (generated first), `summary`,
  `tags{outcome, objections[{type, quote}], lead_temperature}`, `intent`,
  `mood{agent{label,note}, customer{label,note}}`, `next_step`.
- Worker: passes the diarized `call.transcript`; `max_tries=1`, no retry.

## Check A — full pipeline through the worker (happy path)
- `call_2.mp3` uploaded via API → walked `uploaded → transcribing → analyzing
  → completed`.
- Analysis persisted in the v2 shape, coherent and evidence-backed:
  - `outcome=closed_won`, `lead_temperature=hot`, `intent=ready_to_buy`
  - objections: `price` ("man, that 8,300 quite a bit."), `trust`
    ("I'm thinking about scamming.")
  - mood: agent positive / customer neutral, with arc notes.
  - Tags cohere (a hot, ready-to-buy lead who closed, with priced/trust
    objections overcome).

## Check B — analysis failure path (FAULT_INJECT_ANALYSIS=permanent)
- `call_4.mp3` uploaded → `uploaded → transcribing → failed`.
- `status=failed`, `error_code=analysis_failed`.
- **Transcript checkpoint preserved:** transcript present (60 utterances),
  `analysis=null`. A future retry would skip Deepgram and only re-run
  analysis — the never-double-pay property.

## Quality iteration (why the schema is shaped this way)
First-pass output was internally inconsistent (`hot` + `no_clear_outcome` +
a bare `need` tag) and read like sales copy. Fixes:
- **`reasoning` field generated first** — the model justifies before it tags,
  which collapsed the contradictions (on `call_4` it downgraded an unjustified
  `hot`→`warm` and grounded the `need` objection in an actual quote).
- **Evidence-backed objections** (`{type, quote}`) — a tag must point at a line.
- **Mood arc notes** — captures resistant→receptive, which a flat label hid.
- **Neutral-analyst prompt** with per-outcome definitions + tie-break rules.

## Notable findings
- `gemini-2.5-flash` (the planned model) is **retired for new users** — returns
  404 NOT_FOUND despite appearing in `models.list()`. Switched to
  `gemini-3.5-flash`; `gemini-flash-latest` is the always-current alias fallback.
- Error taxonomy reused from M2 with zero new code: real Gemini `429` →
  `provider_rate_limited`, `404` → `analysis_failed`.

## Not covered (deferred to future improvements)
- Repair-retry on validation failure; edge-case hardening (bilingual /
  one-sided / bad-diarization calls); the tagging-eval methodology (golden set,
  periodic review, distribution drift).
