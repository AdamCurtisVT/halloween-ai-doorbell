"""Read-only HA checks by default; explicit opt-in arrival and conditional restore."""
import argparse
from collections import Counter
import uuid
from app.config import Settings
from app.effects.home_assistant import HomeAssistantEffectsAdapter
from app.effects.base import EffectsCoordinator
from app.protect.webhook import csv
from scripts.common import emit, execute


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approve-effects', action='store_true', help='Change designated lights and optionally play the configured stinger')
    args = parser.parse_args()
    settings = Settings()
    if not settings.home_assistant_token.get_secret_value() or not settings.hue_controlled_entities:
        raise ValueError('HA token and explicit controlled light IDs required')
    metrics = Counter()
    adapter = HomeAssistantEffectsAdapter(settings, metrics)
    coordinator = EffectsCoordinator(adapter, metrics, settings.effects_timeout_seconds,
                                      settings.effects_lifecycle_timeout_seconds)
    session_id = uuid.uuid4().hex
    try:
        for entity in csv(settings.hue_controlled_entities):
            state = await adapter.state(entity)
            if state['state'] not in ('on', 'off'):
                raise ValueError('Controlled light unavailable')
        if settings.hue_arrival_scene:
            await adapter.state(settings.hue_arrival_scene)
        if args.approve_effects:
            try:
                coordinator.start(session_id)
                await coordinator.before_speech(session_id)
            finally:
                await coordinator.end(session_id, 'diagnostic')
        emit({'ha_state_reads_succeeded': True, 'effects_dispatched': args.approve_effects,
              'hardware_verified': False, 'observer_review_required': True, 'metrics': dict(metrics)})
        if metrics['effects_degraded'] or metrics['effects_restore_failures']:
            raise RuntimeError('Effects diagnostic incomplete')
    finally:
        await adapter.close()


if __name__ == '__main__':
    execute(main)
