"""Smoke-test the real Deepgram integration: upload a fixture to storage,
presign, transcribe, print the mapped result.

Requires .env pointed at R2 + a real DEEPGRAM_API_KEY.

    cd backend && uv run python scripts/check_deepgram.py <path-to-audio>
    cd backend && uv run python scripts/check_deepgram.py <path-to-audio> --save
    cd backend && uv run python scripts/check_deepgram.py <path-to-audio> --save --out-dir transcripts

With --save, the full result is written as <stem>_transcript.json (contract
shape) and <stem>_transcript.txt (readable, one utterance per line) instead
of being truncated to the console.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.storage import storage
from app.services.transcription import transcribe


def _write_dump(result: dict, audio_path: Path, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = audio_path.stem
    json_path = out_dir / f"{stem}_transcript.json"
    txt_path = out_dir / f"{stem}_transcript.txt"

    json_path.write_text(json.dumps(result, indent=2))

    header = (f"{audio_path.name}  |  language={result['language']}  "
              f"duration={result['duration']:.1f}s  "
              f"utterances={len(result['utterances'])}")
    lines = [header, "=" * len(header), ""]
    for u in result["utterances"]:
        mm, ss = divmod(u["start"], 60)
        lines.append(f"[{int(mm):02d}:{ss:05.2f}] Speaker {u['speaker']}: {u['text']}")
    txt_path.write_text("\n".join(lines) + "\n")
    return txt_path, json_path


async def main(audio_path: Path, save: bool, out_dir: Path) -> None:
    key = f"smoke-{audio_path.name}"
    with open(audio_path, "rb") as f:
        await storage.save(f, key)
    url = await storage.presigned_url(key)
    result = await transcribe(url)
    await storage.delete(key)

    print(f"language={result['language']} duration={result['duration']:.1f}s "
          f"utterances={len(result['utterances'])}")
    assert result["utterances"], "expected at least one utterance"
    assert {"speaker", "start", "end", "text"} <= result["utterances"][0].keys()

    if save:
        txt_path, json_path = _write_dump(result, audio_path, out_dir)
        print(f"saved: {txt_path}")
        print(f"saved: {json_path}")
    else:
        for u in result["utterances"][:5]:
            print(f"  [spk {u['speaker']} {u['start']:6.1f}-{u['end']:6.1f}] {u['text'][:70]}")
        print("shape OK — full result truncated below (use --save for everything)")
        print(json.dumps(result, indent=2)[:2000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, help="path to a .wav or .mp3 file")
    parser.add_argument("--save", action="store_true",
                        help="write full <stem>_transcript.{json,txt} instead of truncating")
    parser.add_argument("--out-dir", type=Path, default=Path("transcripts"),
                        help="directory for --save output (default: transcripts/)")
    args = parser.parse_args()
    asyncio.run(main(args.audio, args.save, args.out_dir))
