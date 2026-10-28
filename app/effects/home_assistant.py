"""Direct REST ownership style. No Hue SDK or concurrent HA script ownership."""
import asyncio
from contextlib import suppress
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx

from app.effects.base import NoOpEffectsAdapter
from app.observability import failure
from app.protect.webhook import csv

# Hue reports the new state shortly after HA answers the service call.
SETTLE_SECONDS = 0.5
STOP_POLLS, STOP_POLL_SECONDS = 6, 0.25
STOPPED = ('idle', 'off', 'standby', 'paused')
APPEARANCE = ('brightness', 'color_mode', 'hs_color', 'xy_color', 'rgb_color', 'rgbw_color',
              'rgbww_color', 'color_temp_kelvin', 'effect')


def appearance(state):
    """What a person sees, without timestamps/context that integrations rewrite on any update."""
    attributes = state.get('attributes') or {}
    return {'state': state.get('state'), **{k: deepcopy(attributes.get(k)) for k in APPEARANCE}}


def context_id(state):
    return (state.get('context') or {}).get('id')


def quiet_hours(value, now):
    if not value:
        return False
    start, end = [tuple(map(int, item.split(':'))) for item in value.split('-')]
    current = now.hour * 60 + now.minute
    begin, finish = start[0] * 60 + start[1], end[0] * 60 + end[1]
    return (begin <= current < finish) if begin < finish else (current >= begin or current < finish)


class HomeAssistantEffectsAdapter(NoOpEffectsAdapter):
    def __init__(self, settings, metrics, client=None, now=None, sleep=asyncio.sleep):
        self.s, self.metrics = settings, metrics
        self.sleep = sleep
        self.client = client or httpx.AsyncClient(
            base_url=settings.home_assistant_url.rstrip('/'),
            headers={'Authorization': 'Bearer ' + settings.home_assistant_token.get_secret_value()},
            timeout=settings.effects_timeout_seconds, follow_redirects=False, trust_env=False)
        self.now = now or (lambda: datetime.now(ZoneInfo(settings.effects_timezone)))
        self.leases = {}
        self.seen = OrderedDict()

    async def state(self, entity):
        response = await self.client.get('/api/states/' + entity)
        response.raise_for_status()
        return response.json()

    async def service(self, domain, action, data):
        """Returns the context IDs of states HA changed during the call: those changes are ours."""
        response = await self.client.post(f'/api/services/{domain}/{action}', json=data)
        response.raise_for_status()
        changed = response.json()
        if isinstance(changed, dict):
            changed = changed.get('changed_states', [])
        return {context_id(state) for state in changed if isinstance(state, dict)} - {None}

    async def on_approach(self, session_id):
        if session_id in self.seen:
            return
        self.seen[session_id] = None
        if len(self.seen) > 128:
            self.seen.popitem(last=False)
        lease = self.leases[session_id] = {'lights': {}, 'contexts': set(), 'speaker_started': False,
                                          'stop_task': None, 'speaker_safe': True, 'speaker_before': None,
                                          'speaker_lock': asyncio.Lock()}
        # Snapshot every designated light before any changes. Do not touch anything on read failure.
        entities = sorted(csv(self.s.hue_controlled_entities))
        before = dict(zip(entities, await asyncio.gather(*(self.state(entity) for entity in entities))))
        if any(state['state'] not in ('on', 'off') for state in before.values()):
            raise ValueError('Controlled light unavailable')
        if self.s.hue_arrival_scene and not self.s.hue_scene_scope_confirmed:
            raise ValueError('Scene porch scope must be confirmed')
        # Unsettled ownership (None) restores unconditionally if the visit ends mid-transition.
        for entity in entities:
            lease['lights'][entity] = (before[entity], None)
        if self.s.hue_arrival_scene:
            lease['contexts'] |= await self.service('scene', 'turn_on', {
                'entity_id': self.s.hue_arrival_scene, 'transition': self.s.effects_transition_seconds})
        else:
            for contexts in await asyncio.gather(*(
                    self.service('light', 'turn_on', {'entity_id': entity,
                        'rgb_color': [170, 65, 255] if index % 2 == 0 else [255, 130, 32],
                        'brightness': max(160, before[entity]['attributes'].get('brightness') or 0),
                        'transition': self.s.effects_transition_seconds})
                    for index, entity in enumerate(entities))):
                lease['contexts'] |= contexts
        await self.sleep(self.s.effects_transition_seconds + SETTLE_SECONDS)
        settled = await asyncio.gather(*(self.state(entity) for entity in entities))
        for entity, after in zip(entities, settled):
            # A new context here is the arrival effect landing (HA keeps the caller's context briefly).
            if context_id(after) not in (None, context_id(before[entity])):
                lease['contexts'].add(context_id(after))
            lease['lights'][entity] = (before[entity], appearance(after))
        if self.s.effects_speaker_enabled and not quiet_hours(self.s.effects_quiet_hours, self.now()):
            await self._stinger(session_id, lease)

    async def _stinger(self, session_id, lease):
        entity = self.s.effects_speaker_entity_id
        current = await self.state(entity)
        if not self.s.effects_speaker_dedicated or current['state'] not in ('idle', 'off', 'standby'):
            self.metrics['effects_speaker_skipped_busy'] += 1
            return
        lease['speaker_before'] = current
        # Mark before dispatch: an HTTP timeout can happen after HA accepted playback.
        lease['speaker_started'] = True
        lease['speaker_safe'] = False
        await self.service('media_player', 'volume_set', {'entity_id': entity,
                           'volume_level': min(self.s.effects_max_volume, current['attributes'].get('volume_level', self.s.effects_max_volume))})
        await self.service('media_player', 'play_media', {'entity_id': entity,
                           'media_content_id': self.s.effects_sound_url, 'media_content_type': 'music'})
        lease['stop_task'] = asyncio.create_task(self._auto_stop(session_id))
        self.metrics['effects_speaker_playbacks'] += 1

    async def _auto_stop(self, session_id):
        await asyncio.sleep(self.s.effects_max_duration_seconds)
        try:
            await self.before_speech(session_id)
        except Exception as error:
            failure('effects_stinger_stop', error)
            self.metrics['effects_speaker_stop_failures'] += 1

    async def before_speech(self, session_id):
        lease = self.leases.get(session_id)
        if not lease or not lease['speaker_started']:
            return True
        task = lease['stop_task']
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        async with lease['speaker_lock']:
            if not lease['speaker_started']:
                return True
            return await self._stop_speaker(lease)

    async def _stop_speaker(self, lease):
        entity = self.s.effects_speaker_entity_id
        state = await self.state(entity)
        media = state['attributes'].get('media_content_id')
        if media and media != self.s.effects_sound_url and state['state'] == 'playing':
            # Another actor took over; don't interrupt it, but never open STT into it.
            return False
        await self.service('media_player', 'media_stop', {'entity_id': entity})
        # The player reports the stop asynchronously; poll briefly before declaring audio unsafe.
        for _ in range(STOP_POLLS):
            stopped = await self.state(entity)
            if stopped['state'] in STOPPED:
                break
            await self.sleep(STOP_POLL_SECONDS)
        else:
            return False
        # Restore volume only when state still matches the setting owned by this effect.
        prior = lease['speaker_before']['attributes'].get('volume_level')
        after_volume = stopped['attributes'].get('volume_level')
        owned_volume = min(self.s.effects_max_volume, lease['speaker_before']['attributes'].get('volume_level', self.s.effects_max_volume))
        if prior is not None and after_volume == owned_volume:
            await self.service('media_player', 'volume_set', {'entity_id': entity, 'volume_level': prior})
        lease['speaker_started'] = False
        lease['speaker_safe'] = True
        self.metrics['effects_speaker_interruptions'] += 1
        return True

    async def on_end(self, session_id, outcome):
        lease = self.leases.get(session_id)
        if not lease:
            return
        try:
            try:
                await self.before_speech(session_id)
            except Exception as error:
                failure('effects_stinger_stop', error)
                self.metrics['effects_speaker_stop_failures'] += 1
            await asyncio.gather(*(self._restore(lease, entity, before, owned)
                                   for entity, (before, owned) in lease['lights'].items()))
        finally:
            task = lease['stop_task']
            if task:
                task.cancel()
            self.leases.pop(session_id, None)

    async def _restore(self, lease, entity, before, owned):
        try:
            current = await self.state(entity)
            if owned is not None and appearance(current) != owned and context_id(current) not in lease['contexts']:
                self.metrics['effects_restore_skipped_manual_change'] += 1
                return
            attrs = before['attributes']
            data = {'entity_id': entity, 'transition': self.s.effects_transition_seconds}
            if before['state'] == 'on':
                if 'brightness' in attrs:
                    data['brightness'] = attrs['brightness']
                mode = attrs.get('color_mode')
                key = {'hs': 'hs_color', 'xy': 'xy_color', 'rgb': 'rgb_color',
                       'rgbw': 'rgbw_color', 'rgbww': 'rgbww_color',
                       'color_temp': 'color_temp_kelvin'}.get(mode)
                if key and key in attrs:
                    data[key] = attrs[key]
                await self.service('light', 'turn_on', data)
            else:
                await self.service('light', 'turn_off', data)
            self.metrics['effects_restore_success'] += 1
        except Exception as error:
            failure('effects_restore', error)
            self.metrics['effects_restore_failures'] += 1

    async def close(self):
        for session_id in list(self.leases):
            await self.on_end(session_id, 'shutdown')
        await self.client.aclose()
