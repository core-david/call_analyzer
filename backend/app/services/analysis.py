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

# Pinned for reproducibility. gemini-2.5-flash is retired for new users;
# "gemini-flash-latest" is the always-current alias if a pin ever 404s.
MODEL = "gemini-3.5-flash"

# One client per process — a thin config wrapper.
_client = genai.Client(api_key=settings.google_api_key)

SYSTEM_INSTRUCTION = (
    "You are a neutral sales-call analyst. You are given a diarized transcript "
    "of a sales call; each line is labeled with an anonymous speaker number. "
    "First infer which speaker is the sales AGENT (represents the company, "
    "drives the call) and which is the CUSTOMER (the prospect) from the "
    "content.\n\n"
    "Write as a neutral analyst: factual, specific, grounded in what was said. "
    "Do NOT use marketing or promotional language (no 'successfully', "
    "'strong desire', 'amazing'). Every judgment must be supported by the "
    "transcript — do not invent facts.\n\n"
    "Fields:\n"
    "- reasoning: 2-4 sentences citing the specific evidence that determines "
    "the tags below. Write this first and let it drive the tags.\n"
    "- summary: 2-3 factual sentences, ALWAYS IN ENGLISH regardless of the "
    "call's language.\n"
    "- tags.outcome — choose exactly one, by what concretely happened:\n"
    "    meeting_scheduled: a specific next call/meeting was agreed.\n"
    "    closed_won: the customer agreed to buy / signed up on the call.\n"
    "    info_requested: the customer asked for materials, a proposal, or "
    "pricing to be sent.\n"
    "    not_interested: the customer declined or opted out.\n"
    "    not_qualified: the customer is not a fit (no budget/need/authority).\n"
    "    no_clear_outcome: none of the above concretely occurred, even if the "
    "call was positive.\n"
    "  Tie-break: pick the most advanced outcome that ACTUALLY happened; "
    "enthusiasm without a concrete next step is no_clear_outcome.\n"
    "- tags.objections: concerns the CUSTOMER raised that could block the "
    "sale, each with a short verbatim quote. Empty list if none. A concern "
    "the agent reframed still counts if the customer voiced it.\n"
    "- tags.lead_temperature: cold (little interest / poor fit), warm "
    "(engaged, evaluating), hot (clear buying signals, ready to move).\n"
    "- intent: the customer's primary intent.\n"
    "- mood.agent / mood.customer: each speaker's dominant mood, and a note on "
    "how it evolved across the call.\n"
    "- next_step: the single most appropriate follow-up action for the agent."
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
