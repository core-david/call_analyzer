"""Generate N valid WAV stub files for bulk upload testing.

Each stub is a 1-second, 16-bit, mono, 16 kHz silent WAV file (~32 KB). They're
valid enough to pass MIME-type and WAV-header checks, so the bulk-ingestion
pipeline can be tested at volume without real audio. Uses only the stdlib.

Usage: python generate_stubs.py [--count 1000] [--output-dir ./audio/stubs]
"""

import argparse
import wave
from pathlib import Path

SAMPLE_RATE = 16000  # Hz
SAMPLE_WIDTH = 2  # bytes (16-bit)
CHANNELS = 1  # mono
DURATION_SECONDS = 1


def generate_stubs(count: int, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # One second of silence: all-zero PCM frames.
    silence = b"\x00" * (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * DURATION_SECONDS)

    total_bytes = 0
    for i in range(1, count + 1):
        path = output_dir / f"stub_{i:04d}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(silence)
        total_bytes += path.stat().st_size

    print(f"Generated {count} WAV stub(s) in {output_dir.resolve()}")
    print(f"Total size: {total_bytes / 1024 / 1024:.1f} MB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000, help="Number of stubs (default: 1000)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent / "audio" / "stubs",
        help="Output directory (default: ./audio/stubs)",
    )
    args = parser.parse_args()
    generate_stubs(args.count, args.output_dir)


if __name__ == "__main__":
    main()
