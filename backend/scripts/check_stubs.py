"""Verify the stub services return the agreed shapes.

Run: cd backend && uv run python scripts/check_stubs.py
"""

import asyncio
import sys
from pathlib import Path

# Put backend/ on sys.path so `app` imports regardless of how this is
# launched (module, direct, or IDE Run button).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.analysis import analyze
from app.services.transcription import transcribe


async def main() -> None:
    t = await transcribe("http://fake-url")
    assert {"language", "text", "utterances"} <= t.keys(), t.keys()
    assert t["utterances"], "expected at least one utterance"
    for u in t["utterances"]:
        assert {"speaker", "start", "end", "text"} <= u.keys(), u

    a = await analyze(t["text"])
    assert {"summary", "tags", "intent", "mood"} <= a.keys(), a.keys()
    assert {"outcome", "objections", "lead_temperature"} <= a["tags"].keys(), a["tags"]

    print("shapes OK")


if __name__ == "__main__":
    asyncio.run(main())