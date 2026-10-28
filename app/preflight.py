"""Safe preflight reports contain check names/results, never configuration values."""
import asyncio
from pathlib import Path
import shutil

from app.audio.input import validate_stream
from app.audio.output import validate_test_wav


def required_fields(settings):
    fields = ['unifi_host', 'unifi_camera_id', 'unifi_username', 'unifi_password',
              'unifi_api_key', 'control_api_token', 'openai_api_key', 'vision_model',
              'dialogue_model', 'tts_model', 'moderation_model', 'fallback_wav', 'goodbye_wav']
    if settings.conversation_mode == 'interactive':
        fields += ['stt_model', 'unifi_audio_stream_url']
    if settings.trigger_source != 'websocket':
        fields += ['unifi_webhook_token', 'unifi_webhook_allowed_camera_ids',
                   'unifi_webhook_allowed_source_ips', 'unifi_webhook_allowed_alarm_names',
                   'unifi_webhook_allowed_triggers']
    return fields


def local_checks(settings, which=shutil.which):
    checks = []
    for name in required_fields(settings):
        value = getattr(settings, name)
        value = value.get_secret_value() if hasattr(value, 'get_secret_value') else value
        checks.append({'check': name.upper(), 'status': 'pass' if value else 'fail',
                       'detail': 'configured' if value else 'required_for_activation'})
    for name in ('fallback_wav', 'goodbye_wav'):
        try:
            validate_test_wav(Path(getattr(settings, name)))
            state = 'pass'
        except Exception:
            state = 'fail'
        checks.append({'check': name.upper() + '_FORMAT', 'status': state, 'detail': 'short_pcm_wav_required'})
    for tool in ('ffmpeg', 'ffprobe'):
        checks.append({'check': tool, 'status': 'pass' if which(tool) else 'fail', 'detail': 'executable_on_path'})
    if settings.conversation_mode == 'interactive':
        try:
            validate_stream(settings.unifi_audio_stream_url.get_secret_value())
            state = 'pass'
        except Exception:
            state = 'fail'
        checks.append({'check': 'microphone_url', 'status': state, 'detail': 'configuration_only_not_auth_proof'})
    checks.append({'check': 'protect_tls', 'status': 'pass' if settings.unifi_verify_ssl else 'warning',
                   'detail': 'verification_enabled' if settings.unifi_verify_ssl else 'verification_disabled'})
    if settings.trigger_source != 'websocket':
        checks.append({'check': 'webhook_token_length',
                       'status': 'pass' if len(settings.unifi_webhook_token.get_secret_value()) >= 32 else 'fail',
                       'detail': 'at_least_32_characters_required'})
    if settings.effects_enabled:
        configured = bool(settings.home_assistant_token.get_secret_value() and settings.hue_controlled_entities)
        checks.append({'check': 'home_assistant_effects', 'status': 'pass' if configured else 'warning',
                       'detail': 'configured_not_hardware_verified' if configured else 'no_HA_degradation_mode'})
        if settings.hue_arrival_scene and not settings.hue_scene_scope_confirmed:
            checks.append({'check': 'hue_scene_scope', 'status': 'warning',
                           'detail': 'scene_refused_until_porch_scope_is_confirmed'})
        if settings.effects_speaker_enabled and (not settings.effects_speaker_dedicated
                or not settings.effects_speaker_entity_id or not settings.effects_sound_url):
            checks.append({'check': 'effects_speaker', 'status': 'warning',
                           'detail': 'stinger_disabled_until_dedicated_player_and_URL_are_configured'})
    return checks


def report(checks):
    return {'ok': not any(c['status'] == 'fail' for c in checks),
            'hardware_verified': False, 'checks': checks}


async def preflight(settings, *, live=False, provider=False, connector=None, client_factory=None, which=shutil.which):
    checks = local_checks(settings, which)
    if live:
        from app.protect.client import connect
        try:
            async with asyncio.timeout(40):
                async with (connector or connect)(settings) as adapter:
                    # No snapshot, subscription, talkback or microphone recording.
                    camera = adapter.camera()
                    checks.append({'check': 'protect_camera', 'status': 'pass', 'detail': 'authenticated_selected_camera'})
                    online = getattr(camera, 'is_connected', None)
                    checks.append({'check': 'camera_online', 'status': 'pass' if online is True else 'fail' if online is False else 'warning',
                                   'detail': 'device_connection_flag_only'})
                    checks.append({'check': 'speaker_capability',
                                   'status': 'pass' if camera.feature_flags.has_speaker else 'fail',
                                   'detail': 'flag_only_not_audibility'})
        except Exception:
            checks.append({'check': 'protect_camera', 'status': 'fail', 'detail': 'connection_permissions_or_camera_selection'})
    if provider:
        from openai import AsyncOpenAI
        client = None
        try:
            client = (client_factory or AsyncOpenAI)(
                api_key=settings.openai_api_key.get_secret_value(),
                timeout=settings.provider_timeout_seconds, max_retries=0)
            names = ['vision_model', 'dialogue_model', 'tts_model', 'moderation_model']
            if settings.conversation_mode == 'interactive':
                names.append('stt_model')
            for name in names:
                try:
                    async with asyncio.timeout(settings.provider_timeout_seconds):
                        await client.models.retrieve(getattr(settings, name))
                    status = 'pass'
                except Exception:
                    status = 'fail'
                checks.append({'check': name.upper() + '_ACCESS', 'status': status,
                               'detail': 'catalog_access_only_not_capability_validation'})
        except Exception:
            checks.append({'check': 'provider_client', 'status': 'fail', 'detail': 'check_api_key_and_dependencies'})
        finally:
            if client:
                try:
                    async with asyncio.timeout(5):
                        await client.close()
                except Exception:
                    checks.append({'check': 'provider_cleanup', 'status': 'fail', 'detail': 'client_close_failed'})
    return report(checks)
