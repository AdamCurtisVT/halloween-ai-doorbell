import asyncio
from collections import Counter
import time
from types import SimpleNamespace as Obj
from fastapi.testclient import TestClient
import pytest
from app.config import Settings
from app.protect.webhook import create_webhook_app, normalize, WebhookInbox
from app.runtime import Runtime


@pytest.fixture
def setup():
    settings = Settings(_env_file=None, unifi_camera_id='selected-camera',
                        unifi_webhook_token='x' * 40, unifi_webhook_allowed_camera_ids='DEVICE-MAC',
                        unifi_webhook_allowed_source_ips='192.0.2.10/32',
                        unifi_webhook_allowed_alarm_names='porch', unifi_webhook_allowed_triggers='person-key')
    metrics = Counter()
    runtime = Obj(enabled=True, connected=True, manager=Obj(), metrics=metrics,
                  inbox=WebhookInbox(settings, metrics))
    client = TestClient(create_webhook_app(settings, runtime), client=('192.0.2.10', 1234))
    payload = {'alarm': {'name': 'porch', 'sources': [], 'conditions': [],
                        'triggers': [{'device': 'DEVICE-MAC', 'key': 'person-key'}]},
               'timestamp': int(time.time() * 1000)}
    return settings, runtime, client, payload


def test_authenticated_scoped_acceptance_and_dedup(setup):
    settings, runtime, client, payload = setup
    headers = {'X-Unifi-Token': 'x' * 40}
    assert client.post('/webhooks/unifi', headers=headers, json=payload).status_code == 202
    assert runtime.inbox.queue.qsize() == 1
    assert client.post('/webhooks/unifi', headers=headers, json=payload).json()['queued'] is False
    assert runtime.inbox.queue.qsize() == 1
    assert runtime.metrics['webhook_duplicates'] == 1
    event = runtime.inbox.queue.get_nowait()
    assert not event.ended and event.action == 'webhook'
    assert 'DEVICE-MAC' not in event.event_ref


def test_auth_and_source_checked_before_json_parsing(setup):
    settings, runtime, client, _ = setup
    response = client.post('/webhooks/unifi', content=b'not-json' * 100000)
    assert response.status_code == 401
    other = TestClient(create_webhook_app(settings, runtime), client=('192.0.2.11', 1234))
    assert other.post('/webhooks/unifi', headers={'X-Unifi-Token': 'x' * 40, 'X-Forwarded-For': '192.0.2.10'}, content=b'bad').status_code == 403
    assert runtime.inbox.queue.empty()


@pytest.mark.parametrize('mutation', ['camera', 'alarm', 'trigger', 'stale', 'future', 'bool_timestamp', 'missing', 'not_object'])
def test_invalid_or_out_of_scope_payloads(setup, mutation):
    _, runtime, client, payload = setup
    if mutation == 'camera': payload['alarm']['triggers'][0]['device'] = 'other'
    if mutation == 'alarm': payload['alarm']['name'] = 'other'
    if mutation == 'trigger': payload['alarm']['triggers'][0]['key'] = 'motion'
    if mutation == 'stale': payload['timestamp'] -= 1000000
    if mutation == 'future': payload['timestamp'] += 1000000
    if mutation == 'bool_timestamp': payload['timestamp'] = True
    if mutation == 'missing': payload = {}
    if mutation == 'not_object': payload = []
    response = client.post('/webhooks/unifi', headers={'X-Unifi-Token': 'x' * 40}, json=payload)
    assert response.status_code == 400
    assert runtime.inbox.queue.empty()


def test_body_limit_and_content_type(setup):
    _, _, client, _ = setup
    headers = {'X-Unifi-Token': 'x' * 40, 'Content-Type': 'application/json'}
    assert client.post('/webhooks/unifi', headers=headers, content=b'x' * 17000).status_code == 413
    assert client.post('/webhooks/unifi', headers={'X-Unifi-Token': 'x' * 40}, content='x').status_code == 415


def test_queue_full_does_not_mark_failed_event_seen(setup):
    settings, runtime, client, payload = setup
    runtime.inbox = WebhookInbox(settings.model_copy(update={'unifi_webhook_queue_size': 1}), runtime.metrics)
    headers = {'X-Unifi-Token': 'x' * 40}
    assert client.post('/webhooks/unifi', headers=headers, json=payload).status_code == 202
    payload['timestamp'] += 11000
    assert client.post('/webhooks/unifi', headers=headers, json=payload).status_code == 429
    runtime.inbox.queue.get_nowait()
    assert client.post('/webhooks/unifi', headers=headers, json=payload).status_code == 202


def test_disabled_runtime_rejects_without_enqueue(setup):
    _, runtime, client, payload = setup
    runtime.enabled = False
    assert client.post('/webhooks/unifi', headers={'X-Unifi-Token': 'x' * 40}, json=payload).status_code == 503
    assert runtime.inbox.queue.empty()


def test_lan_webhook_surface_has_no_management_routes(setup):
    _, _, client, _ = setup
    assert client.get('/status').status_code == 404
    assert client.post('/enable').status_code == 404


async def test_webhook_runtime_dispatches_without_opening_websocket(setup):
    from app.sessions.manager import SessionManager
    from app.sessions.controller import SessionController
    from scripts.simulate import fake_provider
    from unittest.mock import AsyncMock
    settings, _, _, payload = setup
    settings.snapshot_interval_seconds = 0
    # No call to real media, providers or Home Assistant.
    settings.conversation_mode = 'greeting_only'
    controller = SessionController(settings, Obj(snapshot=AsyncMock(return_value=b'jpeg'), play=AsyncMock()), fake_provider())
    manager = SessionManager(settings, controller)
    runtime = Runtime(settings)
    runtime.inbox.enqueue(normalize(payload, settings))
    task = asyncio.create_task(runtime._webhook_events(manager))
    await asyncio.sleep(0)
    await manager.task
    assert controller.metrics['sessions_started'] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
