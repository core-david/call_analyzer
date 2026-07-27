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

# Alias that always resolves to the current Flash model — resists the
# "model retired for new users" 404 that pinned versions eventually hit.
# Pin to a specific version (e.g. "gemini-3.5-flash") for reproducibility.
MODEL = "gemini-flash-latest"

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
