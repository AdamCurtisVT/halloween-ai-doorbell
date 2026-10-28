import argparse
import tempfile
from pathlib import Path
from app.config import Settings
from app.audio.input import FFmpegInput, run_process, phrase_matches
from scripts.common import emit, execute


async def main():
    parser = argparse.ArgumentParser(description="Opt-in incoming microphone recording; separate from talkback")
    parser.add_argument("--seconds", type=int, choices=range(5, 11), default=7)
    parser.add_argument("--approve-recording", action="store_true", required=True)
    parser.add_argument("--expected-phrase", required=True)
    parser.add_argument("--transcriber", required=True, help="Executable: receives WAV path, emits only transcript to stdout")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="doorbell-mic-") as directory:
        wav = Path(directory) / "microphone.wav"
        result = await FFmpegInput().capture(
            Settings().unifi_audio_stream_url.get_secret_value(), args.seconds, wav)
        transcript = (await run_process([args.transcriber, str(wav)], timeout=60, limit=16384)).decode()
        matches = phrase_matches(args.expected_phrase, transcript)
        emit({**result, "known_phrase_matches": matches,
              "transcript_stored": False, "hardware_verification": "operator review required"})
        if not matches:
            raise RuntimeError("Controlled phrase did not match")


if __name__ == "__main__":
    execute(main)
