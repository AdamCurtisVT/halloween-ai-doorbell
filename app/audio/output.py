from pathlib import Path
from typing import Protocol
import wave


class AudioOutputAdapter(Protocol):
    async def play(self, path: Path) -> None: ...


def validate_test_wav(path: Path) -> dict:
    if not 44 <= path.stat().st_size <= 2_000_000:
        raise ValueError("Use a PCM WAV of at most 2 MB")
    with wave.open(str(path), "rb") as audio:
        frames, rate = audio.getnframes(), audio.getframerate()
        channels, width = audio.getnchannels(), audio.getsampwidth()
        duration = frames / rate if rate else 0
        if (audio.getcomptype() != "NONE" or not 0 < duration <= 10
                or channels not in (1, 2) or width not in (1, 2, 3, 4)
                or not 8000 <= rate <= 192000):
            raise ValueError("Use a nonempty mono/stereo PCM WAV no longer than 10 seconds")
        expected_bytes = frames * channels * width
        if expected_bytes > 2_000_000 or len(audio.readframes(frames)) != expected_bytes:
            raise ValueError("WAV audio is truncated or exceeds the size limit")
        return {"seconds": duration, "sample_rate": rate,
                "channels": channels, "sample_width": width}
