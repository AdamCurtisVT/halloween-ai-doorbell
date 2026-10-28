import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import httpx
import pytest

from app.audio.output import validate_test_wav
from app.config import Settings
from app.effects.home_assistant import HomeAssistantEffectsAdapter
from app.preflight import local_checks
from app.protect.webhook import create_webhook_app, WebhookInbox
from app.runtime import Runtime
from scripts.simulate import fixture_wav


def test_wav_rejects_truncated_audio_and_oversized_file(tmp_path):
    path = tmp_path / 'truncated.wav'
    fixture_wav(path)
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError, match='truncated'):
        validate_test_wav(path)
    path.write_bytes(bytes(2_000_001))
    with pytest.raises(ValueError, match='2 MB'):
        validate_test_wav(path)


async def test_shutdown_permanently_blocks_enable_but_disable_does_not():
    runtime = Runtime(Settings(_env_file=None))
    runtime.validate = lambda: None
    async def wait():
        await asyncio.Event().wait()
    runtime._supervise = wait
    await runtime.enable()
    await asyncio.sleep(0)
    await runtime.disable()
    await runtime.enable()
    await asyncio.sleep(0)
    await runtime.close()
    with pytest.raises(ValueError, match='shutting down'):
        await runtime.enable()
    assert runtime.task is None


async def test_disable_during_streamed_body_rejects_at_enqueue_boundary():
    settings = Settings(_env_file=None, unifi_camera_id='camera',
                        unifi_webhook_token='x' * 40, unifi_webhook_allowed_source_ips='192.0.2.10',
                        unifi_webhook_allowed_camera_ids='device', unifi_webhook_allowed_alarm_names='porch',
                        unifi_webhook_allowed_triggers='person')
    runtime = Obj(enabled=True, connected=True, manager=object(), metrics=Counter())
    runtime.inbox = WebhookInbox(settings, runtime.metrics)
    import time
    payload = json.dumps({'timestamp': int(time.time() * 1000), 'alarm': {'name': 'porch',
                         'triggers': [{'device': 'device', 'key': 'person'}]}}).encode()
    async def body():
        yield payload[:10]
        runtime.enabled = False
        yield payload[10:]
    transport = httpx.ASGITransport(app=create_webhook_app(settings, runtime), client=('192.0.2.10', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post('/webhooks/unifi', content=body(),
                                     headers={'X-Unifi-Token': 'x' * 40, 'Content-Type': 'application/json'})
    assert response.status_code == 503
    assert runtime.inbox.queue.empty()


async def test_concurrent_stinger_barriers_stop_only_once():
    settings = Settings(_env_file=None, effects_speaker_entity_id='media_player.porch',
                        effects_sound_url='http://assets.test/sound.wav')
    adapter = HomeAssistantEffectsAdapter(settings, Counter(), client=Obj(aclose=AsyncMock()))
    adapter.leases['session'] = {'speaker_started': True, 'stop_task': None,
                                'speaker_lock': asyncio.Lock()}
    async def stop(lease):
        await asyncio.sleep(0)
        lease['speaker_started'] = False
        return True
    adapter._stop_speaker = AsyncMock(side_effect=stop)
    assert await asyncio.gather(adapter.before_speech('session'), adapter.before_speech('session')) == [True, True]
    adapter._stop_speaker.assert_awaited_once()
    adapter.leases.clear()
    await adapter.close()


@pytest.mark.skipif(not shutil.which('docker'), reason='Compose CLI unavailable')
@pytest.mark.parametrize('overrides,expected', [([], set()),
    (['docker-compose.microphone-secret.yml'], {'UNIFI_AUDIO_STREAM_URL_FILE'}),
    (['docker-compose.ha-secret.yml'], {'HOME_ASSISTANT_TOKEN_FILE'}),
    (['docker-compose.microphone-secret.yml', 'docker-compose.ha-secret.yml'],
     {'UNIFI_AUDIO_STREAM_URL_FILE', 'HOME_ASSISTANT_TOKEN_FILE'})])
def test_optional_compose_secret_mounts_and_empty_quiet_hours(overrides, expected):
    root = Path(__file__).resolve().parents[1]
    command = ['docker', 'compose', '-f', 'docker-compose.secrets.yml']
    for name in overrides:
        command += ['-f', name]
    command += ['config', '--format', 'json']
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True,
                            env={**os.environ, 'UNIFI_HOST': 'example.invalid', 'EFFECTS_QUIET_HOURS': ''})
    service = json.loads(result.stdout)['services']['spike']
    optional = {'UNIFI_AUDIO_STREAM_URL_FILE', 'HOME_ASSISTANT_TOKEN_FILE'}
    assert optional.intersection(service['environment']) == expected
    assert service['environment']['EFFECTS_QUIET_HOURS'] == ''


def test_preflight_explains_disabled_optional_effects():
    settings = Settings(_env_file=None, hue_arrival_scene='scene.porch', effects_speaker_enabled=True)
    warnings = {item['check'] for item in local_checks(settings, which=lambda name: name)
                if item['status'] == 'warning'}
    assert {'home_assistant_effects', 'hue_scene_scope', 'effects_speaker'} <= warnings
