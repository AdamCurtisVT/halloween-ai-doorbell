import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.preflight import preflight, local_checks
from app.protect.snapshots import best_snapshot, quality
from app.runtime import Runtime
from app.sessions.manager import SessionManager
from app.sessions.controller import SessionController
from app.ai.openai_provider import OpenAIProvider
from app.ai.contracts import Costume, Reply
from scripts.control import loopback_url
from scripts.simulate import fixture_wav, fake_provider


@pytest.fixture
def configured(tmp_path):
    wav = tmp_path / 'safe.wav'
    fixture_wav(wav)
    return Settings(_env_file=None, trigger_source='websocket', snapshot_interval_seconds=0, unifi_host='private-console',
                    unifi_camera_id='private-id', unifi_username='private-user',
                    unifi_password='private-password', unifi_api_key='private-key',
                    control_api_token='token', openai_api_key='private-provider-key',
                    vision_model='test-vision', dialogue_model='test-dialogue', tts_model='test-tts',
                    moderation_model='test-moderation', fallback_wav=str(wav), goodbye_wav=str(wav))


async def test_offline_preflight_has_no_network_or_secrets(configured):
    connector, factory = Mock(), Mock()
    result = await preflight(configured, connector=connector, client_factory=factory, which=lambda n: '/bin/' + n)
    assert result['ok'] and result['hardware_verified'] is False
    assert 'private' not in str(result)
    connector.assert_not_called()
    factory.assert_not_called()


async def test_preflight_collects_all_missing_settings_and_tools():
    result = await preflight(Settings(_env_file=None), which=lambda n: None)
    failures = {c['check'] for c in result['checks'] if c['status'] == 'fail'}
    assert {'UNIFI_HOST', 'OPENAI_API_KEY', 'FALLBACK_WAV_FORMAT', 'ffmpeg', 'ffprobe'} <= failures
    assert not result['ok']


async def test_live_preflight_is_read_only_and_checks_catalog(configured):
    adapter = Obj(camera=Mock(return_value=Obj(feature_flags=Obj(has_speaker=True))),
                  snapshot=AsyncMock(), play=AsyncMock())
    @asynccontextmanager
    async def connector(settings):
        yield adapter
    client = Obj(models=Obj(retrieve=AsyncMock()), close=AsyncMock())
    result = await preflight(configured, live=True, provider=True, connector=connector,
                             client_factory=Mock(return_value=client), which=lambda n: n)
    assert result['ok']
    assert client.models.retrieve.await_count == 4
    client.close.assert_awaited_once()
    adapter.snapshot.assert_not_called()
    adapter.play.assert_not_called()


async def test_preflight_errors_are_redacted(configured):
    @asynccontextmanager
    async def connector(settings):
        raise RuntimeError('private credentials in URL')
        yield
    client = Obj(models=Obj(retrieve=AsyncMock(side_effect=RuntimeError('private API error'))), close=AsyncMock())
    result = await preflight(configured, live=True, provider=True, connector=connector,
                             client_factory=Mock(return_value=client), which=lambda n: n)
    assert not result['ok'] and 'private' not in str(result)
    client.close.assert_awaited_once()


async def test_enable_idempotence_disable_and_reenable(configured):
    runtime = Runtime(configured)
    closed = [0]
    async def supervisor():
        try:
            await asyncio.Event().wait()
        finally:
            closed[0] += 1
    runtime._supervise = Mock(side_effect=supervisor)
    await runtime.enable()
    await asyncio.sleep(0)
    task = runtime.task
    await runtime.enable()
    assert runtime.task is task
    await runtime.disable()
    assert closed[0] == 1 and runtime.task is None
    await runtime.enable()
    await asyncio.sleep(0)
    assert runtime.task is not task
    await runtime.close()
    assert closed[0] == 2


def test_all_control_endpoints_require_auth_and_approval():
    runtime = Obj(start=AsyncMock(), close=AsyncMock(), enable=AsyncMock(), disable=AsyncMock(),
                  test_session=Mock(return_value=True), manager=None,
                  status=Mock(return_value={'enabled': False, 'protect_connected': False,
                                            'event_subscription_connected': False, 'mode': 'greeting_only'}))
    app = create_app(Settings(_env_file=None, trigger_source='websocket', control_api_token='token'), runtime)
    headers = {'Authorization': 'Bearer token'}
    with TestClient(app) as client:
        assert client.get('/preflight').status_code == 401
        for endpoint in ('/enable', '/test/session'):
            assert client.post(endpoint).status_code == 401
            assert client.post(endpoint, headers=headers).status_code == 422
            assert client.post(endpoint, headers=headers, json={'approve_camera_audio': False}).status_code == 422
        assert client.post('/enable', headers=headers, json={'approve_camera_audio': True}).status_code == 200
        assert client.post('/test/session', headers=headers, json={'approve_camera_audio': True}).status_code == 202
        runtime.test_session.return_value = False
        assert client.post('/test/session', headers=headers, json={'approve_camera_audio': True}).status_code == 409
        runtime.enable.side_effect = ValueError('private configuration path')
        response = client.post('/enable', headers=headers, json={'approve_camera_audio': True})
        assert response.status_code == 409 and 'private' not in response.text


def test_readiness_waits_for_subscription():
    runtime = Runtime(Settings(_env_file=None, trigger_source='websocket'))
    runtime.enabled = runtime.connected = True
    runtime.protect = Obj(events_connected=False)
    with TestClient(create_app(Settings(_env_file=None, trigger_source='websocket', control_api_token='token'), runtime)) as client:
        assert client.get('/ready', headers={'Authorization': 'Bearer token'}).status_code == 503
        runtime.protect.events_connected = True
        assert client.get('/ready', headers={'Authorization': 'Bearer token'}).status_code == 200


async def test_manual_session_respects_single_session_occupancy_and_cooldown(configured):
    protect = Obj(snapshot=AsyncMock(return_value=b'jpeg'), play=AsyncMock())
    controller = SessionController(configured, protect, fake_provider())
    manager = SessionManager(configured, controller)
    assert manager.request_test()
    assert not manager.request_test()
    await manager.task
    assert not manager.request_test()
    manager.cooldown_until = 0
    manager.last_activity = float('-inf')
    manager.armed = False
    manager.last_activity = manager.clock()  # Inactivity gate, not inferred physical occupancy.
    assert not manager.request_test()
    manager.enabled = False
    assert not manager.request_test()


async def test_frame_selection_retains_best_surviving_snapshot():
    adapter = Obj(snapshot=AsyncMock(side_effect=[b'dark', RuntimeError(), b'clear']))
    assert await best_snapshot(adapter, count=3, interval=0, score=lambda b: len(b)) == b'clear'
    assert adapter.snapshot.await_count == 3
    adapter.snapshot.side_effect = RuntimeError()
    with pytest.raises(ValueError):
        await best_snapshot(adapter, count=3, interval=0)


def test_quality_is_bounded_and_handles_real_jpeg():
    import av
    import io
    frame = av.VideoFrame(64, 64, 'gray')
    for plane in frame.planes:
        plane.update(bytes([128]) * plane.buffer_size)
    output = io.BytesIO()
    with av.open(output, mode='w', format='mjpeg') as container:
        stream = container.add_stream('mjpeg', rate=1)
        stream.width = stream.height = 64
        stream.pix_fmt = 'yuvj420p'
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    assert quality(output.getvalue()) >= 0
    assert quality(b'not jpeg') == -1
    assert quality(bytes(10_000_001)) == -1


@pytest.mark.parametrize('personality', ['haunted_butler', 'friendly_witch', 'silly_skeleton'])
async def test_personas_keep_safety_rules(configured, personality):
    configured.personality = personality
    client = Obj(responses=Obj(parse=AsyncMock(return_value=Obj(output_parsed=Costume.unknown()))))
    provider = OpenAIProvider(configured, client)
    await provider.vision(b'jpeg')
    instructions = client.responses.parse.call_args.kwargs['instructions']
    assert 'never identify' in instructions
    assert 'untrusted data' in instructions


@pytest.mark.parametrize('url', ['https://example.com', 'http://example.com', 'http://127.0.0.1/path',
                                 'http://user:password@127.0.0.1', 'http://localhost:8000'])
def test_control_cli_does_not_send_token_to_remote_or_credentialed_urls(url):
    with pytest.raises(ValueError):
        loopback_url(url)


def test_control_cli_accepts_numeric_loopback():
    assert loopback_url('http://127.0.0.1:8000/') == 'http://127.0.0.1:8000'
    assert loopback_url('http://[::1]:8000') == 'http://[::1]:8000'


async def test_cleanup_errors_are_categorized_not_leaked():
    runtime = Runtime(Settings(_env_file=None))
    operation = AsyncMock(side_effect=RuntimeError('private provider error'))
    await runtime._cleanup(operation())
    assert runtime.metrics['cleanup_failures'] == 1
    assert runtime.last_error == 'resource_cleanup_failed'


async def test_activation_with_invalid_media_returns_configuration_error(configured):
    Path(configured.fallback_wav).write_bytes(b'not a wave file')
    with pytest.raises(ValueError, match='Approved short PCM WAV'):
        await Runtime(configured).enable()
