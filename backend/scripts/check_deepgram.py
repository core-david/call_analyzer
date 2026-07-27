"""Smoke-test the real Deepgram integration: upload a fixture to storage,
presign, transcribe, print the mapped result.

Requires .env pointed at R2 + a real DEEPGRAM_API_KEY.
Run: cd backend && uv run python scripts/check_deepgram.py <path-to-audio>
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.storage import storage
from app.services.transcription import transcribe


async def main(path: str) -> None:
    key = f"smoke-{Path(path).name}"
    with open(path, "rb") as f:
        await storage.save(f, key)
    url = await storage.presigned_url(key)
    result = await transcribe(url)
    await storage.delete(key)

    print(f"language={result['language']} duration={result['duration']:.1f}s "
          f"utterances={len(result['utterances'])}")
    for u in result["utterances"][:5]:
        print(f"  [spk {u['speaker']} {u['start']:6.1f}-{u['end']:6.1f}] {u['text'][:70]}")
    assert result["utterances"], "expected at least one utterance"
    assert {"speaker", "start", "end", "text"} <= result["utterances"][0].keys()
    print("shape OK — full result below")
    print(json.dumps(result, indent=2)[:2000])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
