"""Smoke-test the real Gemini analysis on a saved transcript JSON.

Requires a real GOOGLE_API_KEY in .env.
Produce a transcript first:
    uv run python scripts/check_deepgram.py <audio> --save
Then:
    uv run python scripts/check_gemini.py transcripts/<stem>_transcript.json
    uv run python scripts/check_gemini.py transcripts/<stem>_transcript.json --save

With --save, the analysis is written next to the transcript as
<stem>_analysis.json (the `_transcript` suffix is replaced), instead of only
printing to the console.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.analysis_schema import AnalysisResult
from app.services.analysis import analyze


async def main(transcript_path: Path, save: bool) -> None:
    transcript = json.loads(transcript_path.read_text())
    result = await analyze(transcript)
    # Round-trips through the schema (raises if the shape drifted).
    AnalysisResult.model_validate(result)

    pretty = json.dumps(result, indent=2, ensure_ascii=False)
    print(pretty)

    if save:
        stem = transcript_path.stem.replace("_transcript", "")
        out = transcript_path.with_name(f"{stem}_analysis.json")
        out.write_text(pretty + "\n")
        print(f"\nsaved: {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path, help="path to a *_transcript.json file")
    parser.add_argument("--save", action="store_true",
                        help="write <stem>_analysis.json next to the transcript")
    args = parser.parse_args()
    asyncio.run(main(args.transcript, args.save))
