import asyncio
import json
import wave
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.protect.client import UiprotectAdapter, connect
from app.audio.input import FFmpegInput, DiagnosticError, phrase_matches, run_process, validate_stream
from app.audio.output import validate_test_wav


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def fake_adapter():
    camera = Obj(get_snapshot=AsyncMock(return_value=b'\xff\xd8jpeg'), play_audio=AsyncMock())
    client = Obj(bootstrap=Obj(cameras={'camera': camera}),
                 async_disconnect_ws=AsyncMock(), close_session=AsyncMock())
    return UiprotectAdapter(client, 'camera'), camera, client


def test_api_fail_closed():
    client = TestClient(create_app(settings(control_api_token='private')))
    assert client.get('/health').json()['phase'] == 'development'
    assert client.get('/status').status_code == 401
    assert client.get('/status', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert client.get('/status', headers={'Authorization': 'Bearer private'}).json()['interactive_enabled'] is False
    assert TestClient(create_app(settings())).get('/status').status_code == 401


async def test_snapshot_and_play(tmp_path):
    adapter, camera, _ = fake_adapter()
    assert await adapter.snapshot() == b'\xff\xd8jpeg'
    path = tmp_path / 'approved.wav'
    await adapter.play(path)
    camera.play_audio.assert_awaited_once_with(str(path.resolve()), blocking=True, use_public_api=True)
    camera.get_snapshot.return_value = None
    with pytest.raises(ValueError):
        await adapter.snapshot()


async def test_close_even_when_disconnect_fails():
    adapter, _, client = fake_adapter()
    client.async_disconnect_ws.side_effect = RuntimeError()
    with pytest.raises(RuntimeError):
        await adapter.close()
    client.close_session.assert_awaited_once()


async def test_person_event_filter_queue_and_cleanup():
    adapter, _, client = fake_adapter()
    unsubscribe = Mock()
    def subscribe(callback):
        def event(camera, types):
            return Obj(new_obj=Obj(id='secret-event-id', camera_id=camera,
                       type='smartDetectZone', smart_detect_types=types), action='add')
        callback(event('other-camera', ['person']))
        callback(event('camera', ['vehicle']))
        for _ in range(70):
            callback(event('camera', ['person']))
        return unsubscribe
    client.subscribe_websocket = subscribe
    events = [e async for e in adapter.events(0.01)]
    assert len(events) == 64
    assert adapter.dropped_events == 6
    assert events[0].event_ref != 'secret-event-id'
    assert events[0].smart_types == ('person',)
    unsubscribe.assert_called_once()


async def test_event_cancellation_unsubscribes():
    adapter, _, client = fake_adapter()
    unsubscribe = Mock()
    client.subscribe_websocket = Mock(return_value=unsubscribe)
    stream = adapter.events(30)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unsubscribe.assert_called_once()


async def test_connect_failure_closes_sessions(monkeypatch):
    import uiprotect
    client = Obj(update=AsyncMock(side_effect=RuntimeError()),
                 async_disconnect_ws=AsyncMock(), close_session=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(uiprotect, 'ProtectApiClient', factory)
    with pytest.raises(RuntimeError):
        async with connect(settings(unifi_host='console', unifi_username='user', unifi_password='pass')):
            pass
    assert factory.call_args.kwargs['verify_ssl'] is True
    assert factory.call_args.kwargs['store_sessions'] is False
    client.close_session.assert_awaited_once()


@pytest.mark.parametrize('url', ['http://host/path', 'rtsp://host/path', ''])
def test_reject_uncredentialed_stream(url):
    with pytest.raises(DiagnosticError):
        validate_stream(url)


async def test_microphone_decode(tmp_path):
    runner = AsyncMock(side_effect=[json.dumps({'streams': [
        {'codec_type': 'video'}, {'codec_type': 'audio', 'codec_name': 'aac'}]}).encode(), bytes(5 * 32000)])
    path = tmp_path / 'mic.wav'
    result = await FFmpegInput(runner).capture('rtsps://user:password@camera/stream', 5, path)
    assert result['decoded_seconds'] == 5
    assert result['authentication_verified'] is False
    assert '-map' in runner.call_args.args[0]
    assert validate_test_wav(path)['sample_rate'] == 16000


@pytest.mark.parametrize('responses', [
    [b'{"streams":[]}'],
    [b'{"streams":[{"codec_type":"audio"}]}', b'\x00\x00'],
])
async def test_microphone_no_track_or_early_eof(tmp_path, responses):
    path = tmp_path / 'mic.wav'
    with pytest.raises(DiagnosticError):
        await FFmpegInput(AsyncMock(side_effect=responses)).capture('rtsp://u:p@host/stream', 5, path)
    assert not path.exists()


async def test_process_timeout_and_output_bound():
    import sys
    with pytest.raises(TimeoutError):
        await run_process([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.05)
    with pytest.raises(DiagnosticError):
        await run_process([sys.executable, '-c', 'print("x"*1000)'], timeout=2, limit=100)


def test_wav_limit_and_phrase_match(tmp_path):
    path = tmp_path / 'too-long.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(bytes(11 * 32000))
    with pytest.raises(ValueError):
        validate_test_wav(path)
    assert phrase_matches('Happy Halloween!', 'happy halloween')
    assert not phrase_matches('', '')
    assert not phrase_matches('Happy Halloween', 'hello')


def test_installed_library_contract():
    import inspect
    from uiprotect import ProtectApiClient
    from uiprotect.data import Camera
    assert 'blocking' in inspect.signature(Camera.play_audio).parameters
    assert 'use_public_api' in inspect.signature(Camera.play_audio).parameters
    assert callable(ProtectApiClient.subscribe_websocket)
    assert callable(Camera.get_snapshot)


async def test_capability_report_allowlist():
    adapter, camera, client = fake_adapter()
    camera.id = 'private-camera-id'
    camera.type = 'G4 Doorbell Pro'
    camera.firmware_version = 'test-firmware'
    camera.feature_flags = Obj(has_speaker=True)
    camera.mic_volume = 50
    camera.channels = [Obj(id=0, width=1920, height=1080)]
    camera.host = 'private-host'
    client.bootstrap.nvr = Obj(version='test-protect')
    report = await adapter.report()
    encoded = json.dumps(report)
    assert 'private-camera-id' not in encoded
    assert 'private-host' not in encoded
    assert report['hardware_verified'] is False
    assert report['cameras'][0]['channels'][0]['width'] == 1920


async def test_talkback_failure_stops_stream():
    adapter, camera, _ = fake_adapter()
    camera.talkback_stream = Obj(stop=AsyncMock())
    camera.play_audio.side_effect = RuntimeError('hardware failure')
    with pytest.raises(RuntimeError):
        await adapter.play(Path('approved.wav'))
    camera.talkback_stream.stop.assert_awaited_once()


@pytest.mark.parametrize('name', ['unifi_username', 'unifi_password', 'unifi_api_key',
                                 'unifi_audio_stream_url', 'control_api_token', 'openai_api_key'])
def test_secret_file_loading_and_masking(tmp_path, name):
    path = tmp_path / 'secret'
    path.write_text('private value with spaces \n')
    config = settings(**{name + '_file': str(path)})
    assert getattr(config, name).get_secret_value() == 'private value with spaces '
    assert 'private value with spaces' not in repr(config)


@pytest.mark.parametrize('content', [b'', b'\xff', b'\x00', b'x' * 65537])
def test_secret_file_rejects_invalid_content(tmp_path, content):
    path = tmp_path / 'secret'
    path.write_bytes(content)
    with pytest.raises(ValueError) as error:
        settings(unifi_password_file=str(path))
    assert 'Cannot load nonempty UTF-8 secret' in str(error.value)


def test_secret_file_missing_and_conflicting(tmp_path):
    with pytest.raises(ValueError):
        settings(unifi_password_file=str(tmp_path / 'missing'))
    path = tmp_path / 'secret'
    path.write_text('private-file-password')
    with pytest.raises(ValueError) as error:
        settings(unifi_password='private-env-password', unifi_password_file=str(path))
    assert 'private-env-password' not in str(error.value)
    assert 'private-file-password' not in str(error.value)
    assert settings(unifi_password='', unifi_password_file=str(path)).unifi_password.get_secret_value() == 'private-file-password'


async def test_stalled_playback_times_out_and_stops_stream():
    adapter, camera, _ = fake_adapter()
    adapter.IO_TIMEOUT_SECONDS = 0.01
    camera.talkback_stream = Obj(stop=AsyncMock())
    async def stall(*args, **kwargs):
        await asyncio.Event().wait()
    camera.play_audio.side_effect = stall
    with pytest.raises(TimeoutError):
        await adapter.play(Path('approved.wav'))
    camera.talkback_stream.stop.assert_awaited_once()


async def test_stalled_disconnect_still_closes_session():
    adapter, _, client = fake_adapter()
    adapter.CLEANUP_TIMEOUT_SECONDS = 0.01
    async def stall():
        await asyncio.Event().wait()
    client.async_disconnect_ws.side_effect = stall
    with pytest.raises(TimeoutError):
        await adapter.close()
    client.close_session.assert_awaited_once()


async def test_connection_loss_during_diagnostic_closes(monkeypatch):
    import uiprotect
    adapter, camera, client = fake_adapter()
    client.update = AsyncMock()
    camera.get_snapshot.side_effect = ConnectionError('disconnected')
    monkeypatch.setattr(uiprotect, 'ProtectApiClient', Mock(return_value=client))
    with pytest.raises(ConnectionError):
        async with connect(settings(unifi_host='console', unifi_username='user',
                                    unifi_password='pass', unifi_camera_id='camera')) as active:
            await active.snapshot()
    client.async_disconnect_ws.assert_awaited_once()
    client.close_session.assert_awaited_once()


async def test_cancelled_media_process_is_killed_and_reaped(monkeypatch):
    process = Obj(stdout=Obj(read=AsyncMock()), returncode=None,
                  kill=Mock(), wait=AsyncMock())
    async def stall(*args):
        await asyncio.Event().wait()
    process.stdout.read.side_effect = stall
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    task = asyncio.create_task(run_process(['fake-media'], timeout=30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


def test_local_transcription_uses_only_local_model(tmp_path):
    from scripts.transcribe_local import transcribe
    wav = tmp_path / 'test.wav'
    wav.write_bytes(b'fake')
    model_dir = tmp_path / 'model'
    model_dir.mkdir()
    model = Obj(transcribe=Mock(return_value=(iter([Obj(text=' Happy'), Obj(text='Halloween ')]), None)))
    factory = Mock(return_value=model)
    assert transcribe(wav, model_dir, factory) == 'Happy Halloween'
    factory.assert_called_once_with(str(model_dir.resolve()), device='cpu', compute_type='int8', local_files_only=True)
    with pytest.raises(ValueError):
        transcribe(wav, tmp_path / 'nonexistent', factory)
