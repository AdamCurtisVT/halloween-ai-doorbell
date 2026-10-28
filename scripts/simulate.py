"""Deterministic full-session simulation: no network, camera, microphone, or provider calls."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
import wave

from app.ai.contracts import Costume, Reply
from app.config import Settings
from app.sessions.controller import SessionController


def fixture_wav(path):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        output.writeframes(bytes(3200))


class FakeMicrophone:
    def __init__(self, utterances=None):
        self.healthy = True
        self.gate = False
        self.listen_count = 0
        self.utterances = list(utterances if utterances is not None else [b'pcm'] * 3)

    def close_gate(self):
        self.gate = False

    def open_gate(self):
        self.gate = True

    async def listen(self, seconds):
        assert self.gate
        self.listen_count += 1
        self.gate = False
        return self.utterances.pop(0) if self.utterances else None


def fake_provider():
    async def synthesize(text, path):
        fixture_wav(path)
    return SimpleNamespace(
        vision=AsyncMock(return_value=Costume.unknown()),
        reply=AsyncMock(return_value=Reply(response_text='What a lovely Halloween evening!',
                                          tone='friendly', ends_conversation=False)),
        synthesize=AsyncMock(side_effect=synthesize),
        transcribe=AsyncMock(return_value='I like candy'), close=AsyncMock())


async def main():
    with tempfile.TemporaryDirectory(prefix='doorbell-simulation-') as folder:
        fallback = Path(folder) / 'fallback.wav'
        fixture_wav(fallback)
        settings = Settings(_env_file=None, snapshot_interval_seconds=0, conversation_mode='interactive', audio_echo_guard_ms=0,
                            fallback_wav=str(fallback), goodbye_wav=str(fallback))
        protect = SimpleNamespace(snapshot=AsyncMock(return_value=b'\xff\xd8fake'), play=AsyncMock())
        mic = FakeMicrophone()
        controller = SessionController(settings, protect, fake_provider(), mic)
        await controller.run()
        assert mic.listen_count == 3 and protect.play.await_count == 5
        assert not controller.history and not mic.gate
        print(json.dumps({'simulation': True, 'hardware_verified': False,
                          'state': controller.state, 'metrics': dict(controller.metrics)}))


if __name__ == '__main__':
    asyncio.run(main())
