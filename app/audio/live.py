"""Continuously drain PCM; closed gate discards frames, including during playback."""
import asyncio
from collections import deque
from contextlib import suppress
from urllib.parse import urlsplit
import webrtcvad
from app.audio.input import validate_stream

FRAME_BYTES = 960  # 30 ms, 16 kHz mono s16le; supported by WebRTC VAD.


class MicrophoneFailure(RuntimeError):
    pass


class LiveMicrophone:
    def __init__(self, url, *, vad=None, spawn=asyncio.create_subprocess_exec,
                 aggressiveness=2, onset_ms=210, silence_ms=720):
        validate_stream(url)
        self.url = url
        self.vad = vad or webrtcvad.Vad(aggressiveness)
        self.onset_frames = max(1, (onset_ms + 29) // 30)
        self.silence_frames = max(1, (silence_ms + 29) // 30)
        self.spawn = spawn
        self.frames = asyncio.Queue(maxsize=100)
        self.task = None
        self.gate = False
        self.healthy = False
        self.reconnects = 0
        self.generation = 0

    def clear(self):
        while not self.frames.empty():
            self.frames.get_nowait()

    def close_gate(self):
        self.gate = False
        self.clear()

    def open_gate(self):
        self.clear()
        if not self.healthy:
            raise MicrophoneFailure("Microphone unavailable")
        self.gate = True

    def feed(self, frame):
        if self.gate:
            try:
                self.frames.put_nowait(frame)
            except asyncio.QueueFull:
                self.healthy = False
                self.close_gate()
                raise MicrophoneFailure("Microphone consumer overflow")

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._reader())

    async def _reader(self):
        delay = 1
        while True:
            process = None
            try:
                tls = ["-tls_verify", "1"] if urlsplit(self.url).scheme == "rtsps" else []
                process = await self.spawn(
                    "ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", *tls,
                    "-i", self.url, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                    "-f", "s16le", "pipe:1", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                while True:
                    async with asyncio.timeout(5):
                        frame = await process.stdout.readexactly(FRAME_BYTES)
                    self.healthy = True
                    delay = 1
                    self.feed(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.healthy = False
                self.generation += 1
                self.close_gate()
                self.reconnects += 1
            finally:
                self.healthy = False
                if process is not None:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                    await process.wait()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def listen(self, seconds=8):
        """None = silence; b'' is never returned; transport failure is an exception."""
        generation = self.generation
        onset = deque(maxlen=self.onset_frames)
        utterance = bytearray()
        silence = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        try:
            while loop.time() < deadline:
                if not self.healthy or generation != self.generation:
                    raise MicrophoneFailure("Microphone stream interrupted")
                try:
                    frame = await asyncio.wait_for(self.frames.get(), min(0.2, deadline - loop.time()))
                except TimeoutError:
                    continue
                voiced = self.vad.is_speech(frame, 16000)
                if not utterance:
                    if voiced:
                        onset.append(frame)
                    else:
                        onset.clear()
                    if len(onset) == onset.maxlen:
                        utterance.extend(b"".join(onset))
                else:
                    utterance.extend(frame)
                    silence = 0 if voiced else silence + 1
                    if silence >= self.silence_frames or len(utterance) >= 8 * 32000:  # ~720 ms tail
                        return bytes(utterance[:8 * 32000])
            return bytes(utterance[:8 * 32000]) if utterance else None
        finally:
            self.close_gate()

    async def close(self):
        self.close_gate()
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
