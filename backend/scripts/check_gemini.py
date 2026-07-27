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
