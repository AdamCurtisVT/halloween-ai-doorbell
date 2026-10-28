"""Only this implementation knows uiprotect; no Protect endpoint strings here."""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import AsyncIterator, Protocol

from app.config import Settings


@dataclass(frozen=True)
class PersonEvent:
    event_ref: str
    camera_ref: str
    kind: str
    smart_types: tuple[str, ...]
    action: str
    ended: bool = False


def reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def enum_value(value) -> str:
    return str(getattr(value, "value", value))


class ProtectAdapter(Protocol):
    async def report(self) -> dict: ...
    async def snapshot(self) -> bytes: ...
    async def play(self, path: Path) -> None: ...
    def events(self, seconds: float) -> AsyncIterator[PersonEvent]: ...
    async def close(self) -> None: ...


class UiprotectAdapter:
    IO_TIMEOUT_SECONDS = 30
    CLEANUP_TIMEOUT_SECONDS = 5

    def __init__(self, client, camera_id: str):
        self.client = client
        self.camera_id = camera_id
        self.dropped_events = 0
        self.events_connected = False

    def camera(self):
        if not self.camera_id:
            raise ValueError("Configure UNIFI_CAMERA_ID after local discovery")
        return self.client.bootstrap.cameras[self.camera_id]

    async def report(self) -> dict:
        return {"uiprotect_version": version("uiprotect"),
                "protect_version": str(self.client.bootstrap.nvr.version),
                "hardware_verified": False,
                "cameras": [{"camera_ref": reference(c.id), "model": c.type,
                             "firmware": c.firmware_version,
                             "has_speaker": c.feature_flags.has_speaker,
                             "mic_volume": c.mic_volume,
                             "channels": [{"id": ch.id, "width": ch.width, "height": ch.height}
                                          for ch in c.channels]}
                            for c in self.client.bootstrap.cameras.values()]}

    async def snapshot(self) -> bytes:
        data = await self.camera().get_snapshot()
        if not data or not data.startswith(b"\xff\xd8"):
            raise ValueError("Camera returned no JPEG")
        return data

    async def play(self, path: Path) -> None:
        # 25.0.0: blocking=True awaits stream completion; audible success still needs a human.
        camera = self.camera()
        try:
            async with asyncio.timeout(self.IO_TIMEOUT_SECONDS):
                await camera.play_audio(str(path.resolve()), blocking=True, use_public_api=True)
        finally:
            stream = getattr(camera, "talkback_stream", None)
            if stream is not None:
                async with asyncio.timeout(self.CLEANUP_TIMEOUT_SECONDS):
                    await stream.stop()

    async def events(self, seconds: float) -> AsyncIterator[PersonEvent]:
        queue: asyncio.Queue[PersonEvent] = asyncio.Queue(maxsize=64)

        def receive(message):
            self.events_connected = True
            event = message.new_obj
            if event is None or getattr(event, "camera_id", None) != self.camera_id:
                return
            types = tuple(enum_value(t) for t in getattr(event, "smart_detect_types", ()))
            if "person" not in types:
                return
            item = PersonEvent(reference(event.id), reference(event.camera_id),
                               enum_value(event.type), types, enum_value(message.action),
                               getattr(event, "end", None) is not None)
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                self.dropped_events += 1

        unsubscribe = self.client.subscribe_websocket(receive)
        unsubscribe_state = None
        def connection_changed(state):
            self.events_connected = state.value is True
            # Existing uiprotect contract: CONNECTED.value is True.
            if state.value is not True:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
        try:
            if hasattr(self.client, "subscribe_websocket_state"):
                unsubscribe_state = self.client.subscribe_websocket_state(connection_changed)
            async with asyncio.timeout(seconds):
                while True:
                    item = await queue.get()
                    if item is None:
                        raise ConnectionError("Protect event subscription disconnected")
                    yield item
        except TimeoutError:
            return
        finally:
            self.events_connected = False
            unsubscribe()
            if unsubscribe_state:
                unsubscribe_state()

    async def close(self) -> None:
        try:
            async with asyncio.timeout(self.CLEANUP_TIMEOUT_SECONDS):
                await self.client.async_disconnect_ws()
        finally:
            async with asyncio.timeout(self.CLEANUP_TIMEOUT_SECONDS):
                await self.client.close_session()


@asynccontextmanager
async def connect(settings: Settings):
    from uiprotect import ProtectApiClient
    if not settings.unifi_host or not settings.unifi_username.get_secret_value() or not settings.unifi_password.get_secret_value():
        raise ValueError("Local Protect host/user/password required")
    client = ProtectApiClient(
        settings.unifi_host, settings.unifi_port,
        settings.unifi_username.get_secret_value(), settings.unifi_password.get_secret_value(),
        api_key=settings.unifi_api_key.get_secret_value() or None,
        verify_ssl=settings.unifi_verify_ssl, store_sessions=False,
        cache_dir=Path("/tmp/doorbell-cache"), config_dir=Path("/tmp/doorbell-config"))
    adapter = UiprotectAdapter(client, settings.unifi_camera_id)
    try:
        async with asyncio.timeout(adapter.IO_TIMEOUT_SECONDS):
            await client.update()
        yield adapter
    finally:
        await adapter.close()
