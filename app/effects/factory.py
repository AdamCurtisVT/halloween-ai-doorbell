from app.effects.base import NoOpEffectsAdapter
from app.effects.home_assistant import HomeAssistantEffectsAdapter


def make_effects(settings, metrics):
    if not settings.effects_enabled:
        return NoOpEffectsAdapter()
    if not settings.home_assistant_token.get_secret_value() or not settings.hue_controlled_entities:
        metrics['effects_degraded'] += 1
        return NoOpEffectsAdapter()
    if settings.effects_speaker_enabled and (not settings.effects_speaker_dedicated
            or not settings.effects_speaker_entity_id or not settings.effects_sound_url):
        # Keep safety lighting even with an incomplete optional speaker configuration.
        settings = settings.model_copy(update={'effects_speaker_enabled': False})
        metrics['effects_speaker_configuration_degraded'] += 1
    return HomeAssistantEffectsAdapter(settings, metrics)
