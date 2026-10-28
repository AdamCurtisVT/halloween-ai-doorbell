"""Bounded, opt-in microphone spike; no continuous recording or cloud provider."""
import asyncio
import json
import re
import wave
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


class AudioInputAdapter(Protocol):
    async def capture(self, url: str, seconds: int, destination: Path) -> dict: ...


class DiagnosticError(RuntimeError):
    pass


async def run_process(args: list[str], *, timeout: float, limit: int = 2_000_000) -> bytes:
    # Never echo args, stderr, or underlying exceptions: stream URLs may contain secrets.
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            chunks = bytearray()
            while data := await process.stdout.read(65536):
                chunks.extend(data)
                if len(chunks) > limit:
                    raise DiagnosticError("Diagnostic output exceeded size limit")
            await process.wait()
            if process.returncode:
                raise DiagnosticError("Media/transcriber process failed; inspect configuration privately")
            return bytes(chunks)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


def validate_stream(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise DiagnosticError("Configure a supported RTSP(S) microphone URL")
    if not ((parsed.username and parsed.password) or parsed.query):
        raise DiagnosticError("URL needs credentials or a capability token; verify auth separately")


class FFmpegInput:
    def __init__(self, runner=run_process):
        self.runner = runner

    async def capture(self, url: str, seconds: int, destination: Path) -> dict:
        validate_stream(url)
        if not 5 <= seconds <= 10:
            raise DiagnosticError("Capture must be 5–10 seconds")
        tls = ["-tls_verify", "1"] if urlsplit(url).scheme == "rtsps" else []
        probe = json.loads(await self.runner([
            "ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-show_entries",
            "stream=codec_type,codec_name,sample_rate,channels", "-of", "json", *tls, url], timeout=15))
        tracks = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
        if not tracks:
            raise DiagnosticError("Incoming stream has no audio track")
        pcm = await self.runner([
            "ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", *tls, "-i", url,
            "-t", str(seconds), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-f", "s16le", "pipe:1"], timeout=seconds + 20, limit=seconds * 32000 + 32000)
        if len(pcm) % 2 or len(pcm) < seconds * 32000 * 0.9:
            raise DiagnosticError("Incoming audio ended early or decoded incorrectly")
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(pcm)
        return {"audio_tracks": tracks, "decoded_seconds": len(pcm) / 32000,
                "pcm_format": "16000 Hz mono signed 16-bit", "authentication_verified": False}


def phrase_matches(expected: str, actual: str) -> bool:
    normalize = lambda s: re.findall(r"\w+", s.casefold())
    return bool(normalize(expected)) and normalize(expected) == normalize(actual)
