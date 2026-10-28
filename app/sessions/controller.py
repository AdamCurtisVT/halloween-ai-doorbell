import asyncio
from collections import Counter
from enum import StrEnum
import logging
from pathlib import Path
import tempfile
import time
import uuid

from app.ai.contracts import Costume, Reply
from app.protect.snapshots import best_snapshot
from app.effects.base import EffectsCoordinator, NoOpEffectsAdapter

LOG = logging.getLogger("doorbell.session")


class State(StrEnum):
    IDLE = "IDLE"
    APPROACH_EFFECT = "APPROACH_EFFECT"
    OBSERVING = "OBSERVING"
    GENERATING_GREETING = "GENERATING_GREETING"
    SPEAKING = "SPEAKING"
    LISTENING = "LISTENING"
    PROCESSING_REPLY = "PROCESSING_REPLY"
    CLOSING = "CLOSING"
    COOLDOWN = "COOLDOWN"


class SessionController:
    def __init__(self, settings, protect, provider, microphone=None, effects=None):
        self.settings, self.protect, self.provider, self.microphone = settings, protect, provider, microphone
        self.state = State.IDLE
        self.lock = asyncio.Lock()
        self.metrics = Counter()
        self.effects_adapter = effects or NoOpEffectsAdapter()
        self.effects = None
        self.session_id = None
        self.last_result = "none"
        self.history = []
        self.started_at = 0
        self.first_playback = False

    def transition(self, state):
        self.state = state
        LOG.info('session_state', extra={"session_id": self.session_id, "state": state.value})

    async def call(self, operation, stage):
        start = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                return await operation
        finally:
            self.metrics[stage + "_milliseconds"] += round((time.monotonic() - start) * 1000)

    async def speak(self, text, directory, *, cached=None):
        if self.microphone:
            self.microphone.close_gate()
        if self.effects:
            await self.effects.before_speech(self.session_id)
        generated = True
        if cached is None:
            path = directory / "speech.wav"
            try:
                await self.call(self.provider.synthesize(text, path), "tts")
            except Exception:
                self.metrics["tts_fallbacks"] += 1
                generated = False
                path = Path(self.settings.fallback_wav)
        else:
            path = Path(cached)
        self.transition(State.SPEAKING)
        playback_start = time.monotonic()
        try:
            async with asyncio.timeout(self.settings.playback_timeout_seconds):
                await self.protect.play(path)
        except BaseException:
            self.metrics["playback_failures_or_cancellations"] += 1
            raise
        finally:
            self.metrics["playback_milliseconds"] += round((time.monotonic() - playback_start) * 1000)
        if not self.first_playback:
            self.first_playback = True
            self.metrics["first_greeting_milliseconds"] += round((time.monotonic() - self.started_at) * 1000)
        if self.microphone:
            await asyncio.sleep(self.settings.audio_echo_guard_ms / 1000)
            self.microphone.close_gate()
        self.metrics["playbacks"] += 1
        return generated

    async def run(self):
        if self.lock.locked():
            self.metrics["sessions_busy"] += 1
            return
        async with self.lock:
            self.session_id = uuid.uuid4().hex
            self.started_at = time.monotonic()
            self.first_playback = False
            self.history = []
            self.effects = EffectsCoordinator(self.effects_adapter, self.metrics, self.settings.effects_timeout_seconds)
            self.transition(State.APPROACH_EFFECT)
            self.effects.start(self.session_id)
            self.metrics["sessions_started"] += 1
            try:
                async with asyncio.timeout(self.settings.max_session_seconds):
                    with tempfile.TemporaryDirectory(prefix="doorbell-session-") as folder:
                        await asyncio.sleep(0)  # Dispatch approach effects before beginning camera/AI I/O.
                        await self._conversation(Path(folder))
                self.last_result = "completed"
                self.metrics["sessions_completed"] += 1
            except asyncio.CancelledError:
                self.last_result = "cancelled"
                self.metrics["sessions_cancelled"] += 1
                raise
            except TimeoutError:
                self.last_result = "deadline"
                self.metrics["session_deadlines"] += 1
            except Exception:
                # Never log exception text: library/provider errors can contain private data.
                self.last_result = "failed"
                self.metrics["sessions_failed"] += 1
            finally:
                self.transition(State.CLOSING)
                if self.microphone:
                    self.microphone.close_gate()
                await self.effects.end(self.session_id, self.last_result)
                self.history.clear()
                self.session_id = None
                self.transition(State.COOLDOWN)

    async def _conversation(self, directory):
        self.transition(State.OBSERVING)
        costume = Costume.unknown()
        try:
            async with asyncio.timeout(self.settings.provider_timeout_seconds):
                jpeg = await best_snapshot(self.protect, self.settings.snapshot_count,
                                           self.settings.snapshot_interval_seconds)
                costume = await self.call(self.provider.vision(jpeg), "vision")
        except Exception:
            self.metrics["vision_fallbacks"] += 1
        await self.effects.invoke("on_greeting", self.session_id, costume)
        self.transition(State.GENERATING_GREETING)
        greeting_failed = False
        try:
            greeting = await self.call(self.provider.reply(costume, [], False), "dialogue")
            greeting = Reply.model_validate(greeting.model_dump())
        except Exception:
            greeting_failed = True
            self.metrics["dialogue_fallbacks"] += 1
            greeting = Reply(response_text="Happy Halloween! Welcome to our haunted porch. Have a wonderful evening!",
                             tone="friendly", ends_conversation=True)
        generated = await self.speak(greeting.response_text, directory,
                         cached=self.settings.fallback_wav if greeting_failed else None)
        self.history.append({"role": "assistant", "text": greeting.response_text})
        if (not self.effects.audio_safe or not generated or greeting.ends_conversation or self.settings.conversation_mode == "greeting_only"
                or not self.microphone or not self.microphone.healthy):
            if self.settings.conversation_mode == "interactive" and (not self.microphone or not self.microphone.healthy):
                self.metrics["microphone_degraded"] += 1
            return
        for turn in range(1, self.settings.max_visitor_replies + 1):
            self.transition(State.LISTENING)
            try:
                self.microphone.open_gate()
                pcm = await self.microphone.listen(self.settings.listen_timeout_seconds)
                if pcm is None:
                    self.metrics["no_speech"] += 1
                    return
                self.transition(State.PROCESSING_REPLY)
                transcript = await self.call(self.provider.transcribe(pcm), "stt")
            except Exception:
                self.metrics["microphone_or_stt_failures"] += 1
                return
            finally:
                self.microphone.close_gate()
            if not transcript.strip():
                self.metrics["unintelligible"] += 1
                return
            # Text is untrusted user data, never system instructions; not logged/persisted.
            self.history.append({"role": "visitor", "text": transcript[:1000]})
            self.metrics["visitor_replies"] += 1
            await self.effects.invoke("on_reply", self.session_id, None)
            final = turn == self.settings.max_visitor_replies
            if transcript.strip().lower().rstrip(".!?") in {"bye", "goodbye", "bye bye"}:
                await self.speak("", directory, cached=self.settings.goodbye_wav)
                return
            try:
                reply = await self.call(self.provider.reply(costume, self.history, final), "dialogue")
                reply = Reply.model_validate(reply.model_dump())
            except Exception:
                self.metrics["dialogue_fallbacks"] += 1
                await self.speak("", directory, cached=self.settings.goodbye_wav)
                return
            if not await self.speak(reply.response_text, directory) or not self.effects.audio_safe:
                return
            self.history.append({"role": "assistant", "text": reply.response_text})
            if final or reply.ends_conversation:
                # The controller guarantees a spoken goodbye before teardown, irrespective of flags.
                await self.speak("", directory, cached=self.settings.goodbye_wav)
                return
