import asyncio
from collections import Counter
from contextlib import suppress
import logging

from app.ai.openai_provider import OpenAIProvider
from app.audio.input import validate_stream
from app.audio.live import LiveMicrophone
from app.audio.output import validate_test_wav
from app.observability import RECENT, failure
from app.protect.client import connect
from app.sessions.controller import SessionController
from app.sessions.manager import SessionManager
from pathlib import Path
from app.protect.webhook import WebhookInbox, csv
from app.effects.factory import make_effects


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.manager = None
        self.microphone = None
        self.connected = False
        self.enabled = settings.feature_enabled
        self.last_error = None
        self.reconnects = 0
        self.task = None
        self.metrics = Counter()
        self._control_lock = asyncio.Lock()
        self._closing = False
        self.protect = None
        self.microphone_reconnects = 0
        self.inbox = WebhookInbox(settings, self.metrics)
        self.webhook_listener_ready = False
        self.last_webhook_source_ref = None

    def validate(self):
        s = self.settings
        if s.trigger_source != 'websocket':
            for name in ('unifi_webhook_allowed_camera_ids', 'unifi_webhook_allowed_source_ips',
                         'unifi_webhook_allowed_alarm_names', 'unifi_webhook_allowed_triggers'):
                if not getattr(s, name).strip():
                    raise ValueError('Webhook allowlists required for activation')
            if len(s.unifi_webhook_token.get_secret_value()) < 32:
                raise ValueError('Use at least 32 characters for the webhook token')
            # Every session uses UNIFI_CAMERA_ID; another device's alarm must not greet at the doorbell.
            if len(csv(s.unifi_webhook_allowed_camera_ids)) != 1:
                raise ValueError('List exactly one webhook device: the doorbell')
        for name in ('unifi_host', 'unifi_camera_id', 'vision_model', 'dialogue_model',
                     'tts_model', 'moderation_model', 'fallback_wav', 'goodbye_wav'):
            if not getattr(s, name):
                raise ValueError(f'{name.upper()} required when FEATURE_ENABLED=true')
        for name in ('openai_api_key', 'unifi_api_key', 'control_api_token', 'unifi_username', 'unifi_password'):
            if not getattr(s, name).get_secret_value():
                raise ValueError(f'{name.upper()} required when FEATURE_ENABLED=true')
        try:
            validate_test_wav(Path(s.fallback_wav))
            goodbye = validate_test_wav(Path(s.goodbye_wav))
            if s.arrival_wav:
                validate_test_wav(Path(s.arrival_wav))
        except Exception:
            raise ValueError('Approved short PCM WAV assets are required') from None
        if goodbye['seconds'] + 1 > s.goodbye_reserve_seconds:
            raise ValueError('GOODBYE_RESERVE_SECONDS must exceed the goodbye WAV length by at least 1 second')
        if s.conversation_mode == 'interactive':
            if not s.stt_model:
                raise ValueError('STT_MODEL required for interactive mode')
            validate_stream(s.unifi_audio_stream_url.get_secret_value())

    async def start(self):
        if self.settings.feature_enabled:
            await self.enable()

    async def enable(self):
        async with self._control_lock:
            if self._closing:
                raise ValueError('Runtime is shutting down')
            if self.task is not None and not self.task.done():
                return
            self.validate()
            from app.observability import configure
            configure()
            for name in ('uiprotect', 'openai', 'httpx', 'httpx2', 'aiohttp'):
                logging.getLogger(name).setLevel(logging.CRITICAL + 1)
            for name, logger in list(logging.Logger.manager.loggerDict.items()):
                if isinstance(logger, logging.Logger) and name.startswith(('uiprotect.', 'openai.', 'httpx.', 'httpx2.', 'aiohttp.')):
                    logger.disabled = True
            self.enabled = True
            self.task = asyncio.create_task(self._supervise())

    async def _supervise(self):
        delay = 1
        while True:
            provider = None
            effects = None
            try:
                async with connect(self.settings) as protect:
                    self.protect = protect
                    provider = OpenAIProvider(self.settings)
                    if self.settings.conversation_mode == 'interactive':
                        self.microphone = LiveMicrophone(
                            self.settings.unifi_audio_stream_url.get_secret_value(),
                            aggressiveness=self.settings.vad_aggressiveness,
                            onset_ms=self.settings.vad_onset_ms, silence_ms=self.settings.vad_silence_ms)
                        self.microphone.start()
                    effects = make_effects(self.settings, self.metrics)
                    controller = SessionController(self.settings, protect, provider, self.microphone, effects)
                    controller.metrics = self.metrics
                    manager = SessionManager(self.settings, controller)
                    # After a disconnection, occupancy is unknown: require fresh quiet time.
                    if self.reconnects:
                        manager.armed = False
                        manager.last_activity = manager.clock()
                    manager.enabled = self.enabled
                    self.manager = manager
                    self.connected = True
                    self.last_error = None
                    # Cancel media before closing the Protect client/session.
                    try:
                        if self.settings.trigger_source == 'websocket':
                            await self._websocket_events(protect, manager)
                        elif self.settings.trigger_source == 'webhook':
                            await self._webhook_events(manager)
                        else:
                            async with asyncio.TaskGroup() as group:
                                group.create_task(self._webhook_events(manager))
                                group.create_task(self._websocket_events(protect, manager))
                    finally:
                        await manager.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure('runtime', error)
                self.last_error = 'protect_or_runtime_failure'
                self.reconnects += 1
            finally:
                self.connected = False
                self.protect = None
                self.inbox.clear()
                if self.manager:
                    await self._cleanup(self.manager.cancel())
                    self.manager = None
                if self.microphone:
                    self.microphone_reconnects += self.microphone.reconnects
                    await self._cleanup(self.microphone.close())
                    self.microphone = None
                if effects:
                    await self._cleanup(effects.close())
                if provider:
                    await self._cleanup(provider.close())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _webhook_events(self, manager):
        while True:
            event = await self.inbox.queue.get()
            manager.submit(event)

    async def _websocket_events(self, protect, manager):
        async for event in protect.events(None):
            if protect.dropped_events:
                self.metrics['event_queue_drops'] += protect.dropped_events
                raise ConnectionError('Event queue overflow')
            manager.submit(event)

    async def _cleanup(self, operation):
        try:
            async with asyncio.timeout(10):
                await operation
        except Exception as error:
            failure('cleanup', error)
            self.metrics['cleanup_failures'] += 1
            self.last_error = 'resource_cleanup_failed'

    async def disable(self):
        async with self._control_lock:
            self.enabled = False
            if self.manager:
                self.manager.enabled = False
                await self.manager.cancel()
            if self.task:
                task, self.task = self.task, None
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def test_session(self):
        if not self.enabled or not self.connected or not self.manager:
            return False
        return self.manager.request_test()

    async def close(self):
        self._closing = True
        await self.disable()

    def status(self):
        manager = self.manager
        effective_enabled = self.enabled and (manager is None or manager.enabled)
        return {'hardware_verified': False, 'enabled': effective_enabled,
                'interactive_enabled': effective_enabled and self.settings.conversation_mode == 'interactive',
                'trigger_source': self.settings.trigger_source,
                'webhook_queue_depth': self.inbox.queue.qsize(),
                'webhook_listener_ready': self.webhook_listener_ready,
                'last_webhook_source_ref': self.last_webhook_source_ref,
                'webhook_listener_enabled': self.settings.trigger_source != 'websocket',
                'effects_degraded': bool(self.metrics['effects_degraded']),
                'mode': self.settings.conversation_mode, 'protect_connected': self.connected,
                'event_subscription_connected': bool(self.connected and self.protect and self.protect.events_connected),
                'last_session_result': manager.controller.last_result if manager else None,
                'microphone_healthy': bool(self.microphone and self.microphone.healthy),
                'state': ('IDLE' if manager.can_rearm() else manager.controller.state.value) if manager
                         else 'DISABLED' if not self.enabled else 'CONNECTING',
                'last_error': self.last_error, 'protect_reconnects': self.reconnects,
                'microphone_reconnects': self.microphone_reconnects + (self.microphone.reconnects if self.microphone else 0),
                'recent_failures': list(RECENT), 'metrics': dict(self.metrics)}
