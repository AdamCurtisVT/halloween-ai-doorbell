"""Alarm Manager's documented POST envelope; no invented presence/unique-event fields."""
import asyncio
from collections import OrderedDict
import hashlib
import ipaddress
import json
import secrets
import time
from fastapi import FastAPI, HTTPException, Request
from app.protect.client import PersonEvent, reference


def csv(value):
    return {part.strip() for part in value.split(',') if part.strip()}


def normalize(payload, settings, now=None):
    try:
        alarm = payload['alarm']
        name = alarm['name']
        timestamp = payload['timestamp']
        triggers = alarm['triggers']
        if not isinstance(name, str) or name not in csv(settings.unifi_webhook_allowed_alarm_names):
            raise ValueError()
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError()
        stamp = timestamp / 1000
        if abs((time.time() if now is None else now) - stamp) > settings.unifi_webhook_max_skew_seconds:
            raise ValueError()
        if not isinstance(triggers, list) or not 1 <= len(triggers) <= 16:
            raise ValueError()
        devices = csv(settings.unifi_webhook_allowed_camera_ids)
        keys = csv(settings.unifi_webhook_allowed_triggers)
        events = []
        for trigger in triggers:
            device, key = trigger['device'], trigger['key']
            if not isinstance(device, str) or device not in devices or not isinstance(key, str) or key not in keys:
                raise ValueError()
            camera_ref = reference(settings.unifi_camera_id)
            bucket = int(stamp // settings.unifi_webhook_dedupe_seconds)
            event_ref = hashlib.sha256(json.dumps([camera_ref, name, key, bucket]).encode()).hexdigest()[:24]
            events.append(PersonEvent(event_ref, camera_ref, key, ('person',), 'webhook'))
        # One porch session, even if the envelope repeats matching triggers.
        return list({e.event_ref: e for e in events}.values())
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('Invalid or out-of-scope Alarm Manager event') from None


class WebhookInbox:
    def __init__(self, settings, metrics):
        self.settings, self.metrics = settings, metrics
        self.queue = asyncio.Queue(maxsize=settings.unifi_webhook_queue_size)
        self.seen = OrderedDict()

    def enqueue(self, events):
        now = time.monotonic()
        ttl = self.settings.unifi_webhook_max_skew_seconds * 2 + self.settings.unifi_webhook_dedupe_seconds
        while self.seen and next(iter(self.seen.values())) < now - ttl:
            self.seen.popitem(last=False)
        fresh = [e for e in events if e.event_ref not in self.seen]
        if len(fresh) > self.queue.maxsize - self.queue.qsize():
            self.metrics['webhook_queue_full'] += 1
            raise HTTPException(429, 'Event queue full')
        for event in fresh:
            self.queue.put_nowait(event)
            self.seen[event.event_ref] = now
        while len(self.seen) > 4096:
            self.seen.popitem(last=False)
        self.metrics['webhook_duplicates'] += len(events) - len(fresh)
        self.metrics['webhook_accepted'] += 1
        return bool(fresh)

    def clear(self):
        while not self.queue.empty():
            self.queue.get_nowait()


def create_webhook_app(settings, runtime):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post('/webhooks/unifi', status_code=202)
    async def receive(request: Request):
        try:
            # Source socket only. Proxy forwarding headers are deliberately ignored.
            source = ipaddress.ip_address(request.client.host)
            allowed = any(source in ipaddress.ip_network(net, strict=False)
                          for net in csv(settings.unifi_webhook_allowed_source_ips))
            if not allowed:
                raise HTTPException(403, 'Source not allowed')
            token = settings.unifi_webhook_token.get_secret_value()
            supplied = request.headers.get(settings.unifi_webhook_token_header, '')
            if not token or not secrets.compare_digest(supplied.encode(), token.encode()):
                raise HTTPException(401, 'Authentication required')
            if settings.trigger_source == 'websocket':
                raise HTTPException(503, 'Webhook trigger disabled')
            if not runtime.enabled or not runtime.connected or not runtime.manager:
                raise HTTPException(503, 'Runtime disabled or unavailable')
            if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                raise HTTPException(415, 'JSON required')
            length = request.headers.get('content-length')
            if length is not None and int(length) > settings.unifi_webhook_max_body_bytes:
                raise HTTPException(413, 'Body too large')
            body = bytearray()
            async with asyncio.timeout(3):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > settings.unifi_webhook_max_body_bytes:
                        raise HTTPException(413, 'Body too large')
            events = normalize(json.loads(body), settings)
            # Body reads yield: disable/disconnect may have happened since authentication.
            if not runtime.enabled or not runtime.connected or not runtime.manager:
                raise HTTPException(503, 'Runtime disabled or unavailable')
            queued = runtime.inbox.enqueue(events)
            runtime.last_webhook_source_ref = reference(str(source))
            return {'accepted': True, 'queued': queued}
        except HTTPException:
            runtime.metrics['webhook_rejected'] += 1
            raise
        except (ValueError, TypeError, TimeoutError, RecursionError):
            runtime.metrics['webhook_rejected'] += 1
            raise HTTPException(400, 'Invalid or out-of-scope event') from None

    return app
