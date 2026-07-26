"""LLM analysis service. M1: canned stub. M3 replaces the body with a real
Gemini call behind the exact same signature."""

import asyncio

STUB_DELAY_SECONDS = 2


async def analyze(transcript_text: str) -> dict:
    """Analyze a call transcript into the closed tagging schema.

    Returns: {summary, tags: {outcome, objections, lead_temperature},
              intent, mood: {agent, customer}}
    """
    await asyncio.sleep(STUB_DELAY_SECONDS)  # simulate provider latency
    return {
        "summary": "Cold outreach call. Prospect showed interest but "
                   "flagged price sensitivity; asked for details by email.",
        "tags": {
            "outcome": "follow_up",
            "objections": ["price"],
            "lead_temperature": "warm",
        },
        "intent": "evaluate_product",
        "mood": {"agent": "friendly", "customer": "neutral"},
    }