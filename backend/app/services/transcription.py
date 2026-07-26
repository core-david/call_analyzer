"""Transcription service. M1: canned stub. M2 replaces the body with a real
Deepgram call behind the exact same signature."""

import asyncio

STUB_DELAY_SECONDS = 2


async def transcribe(audio_url: str) -> dict:
    """Transcribe the audio at `audio_url` with speaker diarization.

    Returns: {language, text, utterances: [{speaker, start, end, text}]}
    """
    await asyncio.sleep(STUB_DELAY_SECONDS)  # simulate provider latency
    utterances = [
        {"speaker": 0, "start": 0.0, "end": 3.5,
         "text": "Hi, this is Ana calling from Altur, do you have a minute?"},
        {"speaker": 1, "start": 3.9, "end": 6.1,
         "text": "Sure, what is this about?"},
        {"speaker": 0, "start": 6.4, "end": 11.0,
         "text": "We help teams analyze their sales calls automatically."},
        {"speaker": 1, "start": 11.4, "end": 14.2,
         "text": "Interesting — send me the details, price matters though."},
    ]
    return {
        "language": "en",
        "text": " ".join(u["text"] for u in utterances),
        "utterances": utterances,
    }