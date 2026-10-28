import argparse
import asyncio
from pathlib import Path
from app.config import Settings
from app.protect.client import connect
from app.audio.output import validate_test_wav
from scripts.common import emit, execute


async def main():
    parser = argparse.ArgumentParser(description="Plays one approved short WAV on the real speaker")
    parser.add_argument("wav", type=Path)
    parser.add_argument("--approve-playback", action="store_true", required=True)
    args = parser.parse_args()
    audio = validate_test_wav(args.wav)
    settings = Settings()
    if not settings.unifi_api_key.get_secret_value():
        raise ValueError("Public talkback API key required")
    async with connect(settings) as adapter:
        async with asyncio.timeout(30):
            await adapter.play(args.wav)
        emit({"library_playback_completed": True, "audible_hardware_verification": "pending",
              "input_format": audio})


if __name__ == "__main__":
    execute(main)
