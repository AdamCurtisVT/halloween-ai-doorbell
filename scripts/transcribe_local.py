"""Optional offline STT helper for the Phase A controlled-phrase diagnostic."""
import argparse
import logging
import os
from pathlib import Path
import sys


def transcribe(wav: Path, model_dir: Path, factory=None) -> str:
    if not wav.is_file() or not model_dir.is_dir():
        raise ValueError("Provide a WAV and an existing local converted model directory")
    if factory is None:
        from faster_whisper import WhisperModel
        factory = WhisperModel
    model = factory(str(model_dir.resolve()), device="cpu", compute_type="int8",
                    local_files_only=True)
    segments, _ = model.transcribe(str(wav), beam_size=5, vad_filter=False)
    return " ".join(segment.text.strip() for segment in segments).strip()


def main():
    parser = argparse.ArgumentParser(description="Offline diagnostic STT; stdout is transcript only")
    parser.add_argument("wav", type=Path)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        location = os.environ.get("STT_MODEL_DIR", "")
        if not location:
            raise ValueError("STT_MODEL_DIR required")
        print(transcribe(args.wav, Path(location)))
    except Exception:
        print("Local transcription failed; check optional dependencies and local model setup.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
