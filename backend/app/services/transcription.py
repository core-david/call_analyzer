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
            diarize_model="v2",      # enables diarization AND pins the batch diarizer
            utterances=True,
            detect_language=True,
            # arq owns retries — SDK-internal retries would multiply load on a
            # struggling provider (tries x retries). Leave SDK retries off.
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
    """Map Deepgram's response into the M1 transcript contract.

    Field paths verified against deepgram-sdk v7 typed models:
    results.channels[0].{detected_language, alternatives[0].transcript},
    results.utterances[].{speaker, start, end, transcript}, metadata.duration.
    """
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
