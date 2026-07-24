# Test Audio Fixtures

This directory holds audio used by the test suite. Two kinds:

1. **Real recordings** — for verifying transcription quality and speaker
   diarization against genuine multi-speaker speech.
2. **WAV stubs** — tiny valid WAV files for exercising the bulk-upload pipeline.

> **Audio files are gitignored.** Everything under `audio/` is excluded from git
> (see the repo `.gitignore`). Only this README and `generate_stubs.py` are
> committed.

Layout:

```
audio/
  recordings/   real downloaded calls
  stubs/        generated WAV stubs
```

---

## Real recordings

Sourced from Creative Commons–licensed sales-call role-plays on YouTube,
downloaded into `audio/recordings/`. These give us representative two-party
sales conversations for testing transcription and diarization.

Format notes: the pipeline targets 16 kHz mono for transcription. Convert a
recording if needed:

```bash
ffmpeg -i input.mp3 -ac 1 -ar 16000 audio/recordings/sample_01.wav
```

---

## WAV stubs

`generate_stubs.py` writes 1-second, 16-bit, mono, 16 kHz **silent** WAV files
using Python's stdlib `wave` module (no external dependencies). Each file is
~32 KB (~32 MB for 1,000) and is valid enough to pass MIME-type and WAV-header
checks — enough to test that bulk ingestion handles volume.

```bash
# from backend/
uv run python tests/fixtures/generate_stubs.py                 # 1000 files → tests/fixtures/audio/stubs/
uv run python tests/fixtures/generate_stubs.py --count 5       # quick smoke test
uv run python tests/fixtures/generate_stubs.py --output-dir /tmp/stubs
```

Files are named `stub_0001.wav` … `stub_1000.wav`, land in `audio/stubs/` by
default, and are gitignored.

> The upload pipeline accepts both WAV and MP3. The stubs are WAV-only for now;
> MP3 decoding is exercised by the real recordings (downloaded as MP3). Bulk
> MP3 stubs can be added later if we need to test the MP3 accept-path at volume.
