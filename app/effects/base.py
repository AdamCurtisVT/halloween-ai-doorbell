import asyncio
import time
from typing import Protocol


class EffectsAdapter(Protocol):
    async def on_approach(self, session_id): ...
    async def on_greeting(self, session_id, costume): ...
    async def on_reply(self, session_id, sentiment=None): ...
    async def on_end(self, session_id, outcome): ...
    async def before_speech(self, session_id) -> bool: ...


class NoOpEffectsAdapter:
    async def on_approach(self, session_id): pass
    async def on_greeting(self, session_id, costume): pass
    async def on_reply(self, session_id, sentiment=None): pass
    async def on_end(self, session_id, outcome): pass
    async def before_speech(self, session_id): return True
    async def close(self): pass


class EffectsCoordinator:
    def __init__(self, adapter, metrics, timeout=3):
        self.adapter, self.metrics, self.timeout = adapter, metrics, timeout
        self.approach = None
        self.audio_safe = True

    async def invoke(self, method, *args):
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout):
                result = await getattr(self.adapter, method)(*args)
            self.metrics['effects_' + method + '_success'] += 1
            return result
        except Exception:
            self.metrics['effects_degraded'] += 1
            return False
        finally:
            self.metrics['effects_' + method + '_milliseconds'] += round((time.monotonic() - started) * 1000)

    def start(self, session_id):
        self.audio_safe = True
        self.approach = asyncio.create_task(self.invoke('on_approach', session_id))

    async def before_speech(self, session_id):
        if self.approach:
            await self.approach  # Its own bounded timeout, never part of webhook acknowledgement.
            self.approach = None
        safe = await self.invoke('before_speech', session_id)
        self.audio_safe = self.audio_safe and safe is not False
        return self.audio_safe

    async def end(self, session_id, outcome):
        if self.approach:
            self.approach.cancel()
            try:
                await self.approach
            except asyncio.CancelledError:
                pass
            self.approach = None
        await self.invoke('on_end', session_id, outcome)
