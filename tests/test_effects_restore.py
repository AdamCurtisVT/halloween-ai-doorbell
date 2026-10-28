import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import httpx
import pytest
from app.config import Settings
from app.effects.home_assistant import HomeAssistantEffectsAdapter, quiet_hours
from app.effects.factory import make_effects
from app.effects.base import NoOpEffectsAdapter
from app.sessions.controller import SessionController
from scripts.simulate import fake_provider, FakeMicrophone, fixture_wav
from pathlib import Path


@pytest.fixture
def ha():
    settings = Settings(_env_file=None, home_assistant_url='http://ha.test', home_assistant_token='secret',
                        hue_controlled_entities='light.porch', effects_quiet_hours='',
                        effects_speaker_entity_id='media_player.porch', effects_sound_url='http://assets.test/owned.wav')
    states = {'light.porch': {'state': 'on', 'attributes': {'brightness': 153, 'color_mode': 'rgb', 'rgb_color': [255, 200, 140]},
                              'last_updated': 'before', 'context': {'id': 'before'}},
              'media_player.porch': {'state': 'idle', 'attributes': {'volume_level': 0.5},
                                     'context': {'id': 'before'}, 'last_updated': 'before'}}
    calls = []
    async def handler(request):
        assert request.headers['Authorization'] == 'Bearer secret'
        path = request.url.path
        if request.method == 'GET':
            return httpx.Response(200, json=deepcopy(states[path.split('/')[-1]]))
        data = json.loads(request.content)
        calls.append((path, data))
        entity = data['entity_id']
        state = states.get(entity)
        if path.endswith('/light/turn_on'):
            state['state'] = 'on'
            state['attributes'].update({k: v for k, v in data.items() if k not in ('entity_id', 'transition')})
        elif path.endswith('/light/turn_off'):
            state['state'] = 'off'
        elif path.endswith('/media_player/play_media'):
            state['state'] = 'playing'
            state['attributes']['media_content_id'] = data['media_content_id']
        elif path.endswith('/media_player/media_stop'):
            state['state'] = 'idle'
        elif path.endswith('/media_player/volume_set'):
            state['attributes']['volume_level'] = data['volume_level']
        if state:
            state['context'] = {'id': 'call-' + str(len(calls))}
            state['last_updated'] = str(len(calls))
        return httpx.Response(200, json=[])
    client = httpx.AsyncClient(base_url=settings.home_assistant_url,
                              headers={'Authorization': 'Bearer secret'}, transport=httpx.MockTransport(handler))
    adapter = HomeAssistantEffectsAdapter(settings, Counter(), client=client, sleep=AsyncMock())
    return settings, states, calls, adapter


async def test_approach_idempotence_and_prior_light_restore(ha):
    _, states, calls, adapter = ha
    before = deepcopy(states['light.porch']['attributes'])
    await adapter.on_approach('session')
    await adapter.on_approach('session')
    assert len(calls) == 1
    assert states['light.porch']['attributes']['brightness'] >= 160
    await adapter.on_end('session', 'completed')
    await adapter.on_end('session', 'completed')
    assert states['light.porch']['attributes'] == before
    assert adapter.metrics['effects_restore_success'] == 1
    await adapter.close()


async def test_manual_changes_survive_restore(ha):
    _, states, calls, adapter = ha
    await adapter.on_approach('session')
    states['light.porch']['attributes']['brightness'] = 230
    states['light.porch']['context'] = {'id': 'manual'}
    await adapter.on_end('session', 'timeout')
    assert states['light.porch']['attributes']['brightness'] == 230
    assert len(calls) == 1
    assert adapter.metrics['effects_restore_skipped_manual_change'] == 1
    await adapter.close()


async def test_stinger_stops_and_restores_volume_before_speech(ha):
    settings, states, calls, adapter = ha
    settings.effects_speaker_enabled = settings.effects_speaker_dedicated = True
    await adapter.on_approach('session')
    assert states['media_player.porch']['state'] == 'playing'
    assert states['media_player.porch']['attributes']['volume_level'] <= 0.30
    assert await adapter.before_speech('session')
    assert states['media_player.porch']['state'] == 'idle'
    assert states['media_player.porch']['attributes']['volume_level'] == 0.5
    await adapter.on_end('session', 'completed')
    await adapter.close()


async def test_existing_household_media_is_not_interrupted(ha):
    settings, states, calls, adapter = ha
    settings.effects_speaker_enabled = settings.effects_speaker_dedicated = True
    states['media_player.porch']['state'] = 'playing'
    await adapter.on_approach('session')
    assert not any('media_player' in path for path, _ in calls)
    await adapter.on_end('session', 'completed')
    await adapter.close()


async def test_quiet_hours_leave_safety_lighting_enabled(ha):
    settings, _, calls, adapter = ha
    settings.effects_speaker_enabled = settings.effects_speaker_dedicated = True
    settings.effects_quiet_hours = '22:00-09:00'
    adapter.now = lambda: datetime(2026, 10, 28, 23, 0)
    await adapter.on_approach('session')
    assert len(calls) == 1 and calls[0][0].endswith('/light/turn_on')
    await adapter.close()


async def test_unknown_speaker_stop_never_allows_stt(tmp_path):
    wav = tmp_path / 'safe.wav'
    fixture_wav(wav)
    settings = Settings(_env_file=None, snapshot_interval_seconds=0, conversation_mode='interactive',
                        audio_echo_guard_ms=0, fallback_wav=str(wav), goodbye_wav=str(wav))
    effects = Obj(on_approach=AsyncMock(), on_greeting=AsyncMock(), on_reply=AsyncMock(),
                  on_end=AsyncMock(), before_speech=AsyncMock(return_value=False))
    mic = FakeMicrophone()
    protect = Obj(snapshot=AsyncMock(return_value=b'jpeg'), play=AsyncMock())
    controller = SessionController(settings, protect, fake_provider(), mic, effects)
    await controller.run()
    assert protect.play.await_count == 1  # Doorbell still degrades to a greeting.
    assert mic.listen_count == 0
    effects.on_end.assert_awaited_once()


async def test_effects_begin_before_inference_completes_and_ha_failure_does_not_block(tmp_path):
    wav = tmp_path / 'safe.wav'
    fixture_wav(wav)
    settings = Settings(_env_file=None, snapshot_interval_seconds=0, fallback_wav=str(wav), goodbye_wav=str(wav))
    approached = asyncio.Event()
    effects = NoOpEffectsAdapter()
    async def approach(session_id):
        approached.set()
        raise ConnectionError('private HA URL')
    effects.on_approach = approach
    provider = fake_provider()
    original = provider.greet.return_value
    async def vision(jpeg):
        await approached.wait()
        return original
    provider.greet.side_effect = vision
    protect = Obj(snapshot=AsyncMock(return_value=b'jpeg'), play=AsyncMock())
    controller = SessionController(settings, protect, provider, effects=effects)
    await controller.run()
    assert controller.metrics['effects_degraded'] == 1
    assert protect.play.await_count == 1


def test_no_ha_and_disabled_effects_degrade_without_blocking_lights_policy():
    metrics = Counter()
    assert isinstance(make_effects(Settings(_env_file=None), metrics), NoOpEffectsAdapter)
    assert metrics['effects_degraded'] == 1
    assert quiet_hours('22:00-09:00', datetime(2026, 10, 28, 8))
    assert not quiet_hours('22:00-09:00', datetime(2026, 10, 28, 12))


async def test_stinger_auto_stop_has_bounded_duration(ha):
    settings, states, _, adapter = ha
    settings.effects_speaker_enabled = settings.effects_speaker_dedicated = True
    settings.effects_max_duration_seconds = 0.01
    await adapter.on_approach('session')
    await asyncio.sleep(0.03)
    assert states['media_player.porch']['state'] == 'idle'
    await adapter.close()


async def test_restore_when_only_context_changed_but_light_looks_owned(ha):
    _, states, calls, adapter = ha
    before = deepcopy(states['light.porch']['attributes'])
    await adapter.on_approach('session')
    # HA restart or an integration refresh rewrites context/last_updated without a visible change.
    states['light.porch']['context'] = {'id': 'after-ha-restart'}
    states['light.porch']['last_updated'] = 'later'
    await adapter.on_end('session', 'completed')
    assert len(calls) == 2
    assert states['light.porch']['attributes'] == before
    await adapter.close()


async def test_restore_when_hue_reports_state_after_the_service_call(ha):
    _, states, calls, adapter = ha
    land = defer_state_updates(adapter, states)
    before = deepcopy(states['light.porch']['attributes'])
    await adapter.on_approach('session')
    assert states['light.porch']['attributes']['brightness'] >= 160
    await adapter.on_end('session', 'completed')
    await land()
    assert adapter.metrics['effects_restore_success'] == 1
    assert states['light.porch']['attributes'] == before
    await adapter.close()


async def test_stinger_stop_reported_late_still_allows_listening(ha):
    settings, states, calls, adapter = ha
    settings.effects_speaker_enabled = settings.effects_speaker_dedicated = True
    land = defer_state_updates(adapter, states)
    await adapter.on_approach('session')
    await land()
    assert states['media_player.porch']['state'] == 'playing'
    assert await adapter.before_speech('session')
    assert states['media_player.porch']['state'] == 'idle'
    await adapter.on_end('session', 'completed')
    await adapter.close()


def defer_state_updates(adapter, states):
    """Real HA answers a service call before the integration reports the new state.
    Service calls change `truth`; reads see it only after the adapter waits (or the test lands it)."""
    truth = deepcopy(states)
    handler = adapter.client._transport.handler
    async def late(request):
        if request.method == 'GET':
            return await handler(request)
        visible = deepcopy(states)
        states.clear()
        states.update(deepcopy(truth))
        response = await handler(request)
        truth.clear()
        truth.update(deepcopy(states))
        states.clear()
        states.update(visible)
        return response
    async def land(*args):
        states.clear()
        states.update(deepcopy(truth))
    adapter.client._transport.handler = late
    adapter.sleep = land
    return land


async def test_same_session_from_multiple_sources_does_not_duplicate_effects(ha, tmp_path):
    from app.sessions.manager import SessionManager
    from app.protect.client import PersonEvent
    settings, _, calls, adapter = ha
    wav = tmp_path / 'fallback.wav'
    fixture_wav(wav)
    settings.fallback_wav = settings.goodbye_wav = str(wav)
    settings.snapshot_interval_seconds = 0
    controller = SessionController(settings, Obj(snapshot=AsyncMock(return_value=b'jpeg'), play=AsyncMock()),
                                   fake_provider(), effects=adapter)
    manager = SessionManager(settings, controller)
    manager.submit(PersonEvent('webhook-id', 'camera', 'person', ('person',), 'webhook'))
    manager.submit(PersonEvent('ws-id', 'camera', 'person', ('person',), 'add'))
    await manager.task
    assert len([c for c in calls if c[0].endswith('/light/turn_on')]) == 2  # One arrival + one restore.
    assert controller.metrics['sessions_started'] == 1
    await adapter.close()


async def test_cancellation_restores_effects_in_finally(tmp_path):
    wav = tmp_path / 'safe.wav'
    fixture_wav(wav)
    settings = Settings(_env_file=None, snapshot_interval_seconds=0, fallback_wav=str(wav), goodbye_wav=str(wav))
    began = asyncio.Event()
    effects = NoOpEffectsAdapter()
    async def approach(session_id):
        began.set()
    effects.on_approach = approach
    effects.on_end = AsyncMock()
    async def stall(jpeg):
        await asyncio.Event().wait()
    provider = fake_provider()
    provider.greet.side_effect = stall
    controller = SessionController(settings, Obj(snapshot=AsyncMock(return_value=b'jpeg')), provider, effects=effects)
    task = asyncio.create_task(controller.run())
    await began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    effects.on_end.assert_awaited_once()
    assert effects.on_end.call_args.args[1] == 'cancelled'


async def test_arrival_and_end_get_the_longer_effects_limit():
    from app.effects.base import EffectsCoordinator
    adapter = NoOpEffectsAdapter()
    async def slow(*args):
        await asyncio.sleep(0.05)
    adapter.on_approach = adapter.on_reply = slow
    metrics = Counter()
    coordinator = EffectsCoordinator(adapter, metrics, timeout=0.01, lifecycle_timeout=1)
    await coordinator.invoke('on_approach', 'session')
    await coordinator.invoke('on_reply', 'session')
    assert metrics['effects_on_approach_success'] == 1
    assert metrics['effects_degraded'] == 1


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
