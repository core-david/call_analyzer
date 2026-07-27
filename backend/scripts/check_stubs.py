"""Verify the analysis stub returns the agreed shape.

Transcription is a real Deepgram integration since M2 — its shape is
verified by scripts/check_deepgram.py, not here.

Run: cd backend && uv run python scripts/check_stubs.py
"""

import asyncio
import sys
from pathlib import Path

# Put backend/ on sys.path so `app` imports regardless of how this is
# launched (module, direct, or IDE Run button).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.analysis import analyze


async def main() -> None:
    a = await analyze("A short sample sales-call transcript for shape checking.")
    assert {"summary", "tags", "intent", "mood"} <= a.keys(), a.keys()
    assert {"outcome", "objections", "lead_temperature"} <= a["tags"].keys(), a["tags"]

    print("analysis shape OK")


if __name__ == "__main__":
    asyncio.run(main())