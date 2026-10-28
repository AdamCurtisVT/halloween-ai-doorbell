from pathlib import Path
from pydantic import SecretStr, Field, model_validator
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    unifi_host: str = ""
    unifi_port: int = 443
    unifi_username: SecretStr = SecretStr("")
    unifi_password: SecretStr = SecretStr("")
    unifi_api_key: SecretStr = SecretStr("")
    unifi_camera_id: str = ""
    unifi_verify_ssl: bool = True
    unifi_audio_stream_url: SecretStr = SecretStr("")
    control_api_token: SecretStr = SecretStr("")

    trigger_source: Literal["webhook", "websocket", "webhook_with_websocket_fallback"] = "webhook"
    unifi_webhook_token: SecretStr = SecretStr("")
    unifi_webhook_token_file: str = ""
    unifi_webhook_token_header: str = "X-Unifi-Token"
    unifi_webhook_allowed_camera_ids: str = ""
    unifi_webhook_allowed_source_ips: str = ""
    unifi_webhook_allowed_alarm_names: str = ""
    unifi_webhook_allowed_triggers: str = ""
    unifi_webhook_max_body_bytes: int = Field(default=16384, ge=512, le=65536)
    unifi_webhook_max_skew_seconds: int = Field(default=120, ge=1, le=600)
    unifi_webhook_dedupe_seconds: int = Field(default=10, ge=1, le=60)
    unifi_webhook_queue_size: int = Field(default=32, ge=1, le=256)
    home_assistant_url: str = "http://homeassistant.local:8123"
    home_assistant_token: SecretStr = SecretStr("")
    home_assistant_token_file: str = ""
    effects_enabled: bool = True
    hue_arrival_scene: str = ""
    hue_scene_scope_confirmed: bool = False
    hue_controlled_entities: str = ""
    hue_restore_mode: Literal["snapshot_conditional"] = "snapshot_conditional"
    effects_timeout_seconds: float = Field(default=3, gt=0, le=10)
    effects_transition_seconds: float = Field(default=1, ge=0.5, le=1.5)
    effects_speaker_enabled: bool = False
    effects_speaker_dedicated: bool = False
    effects_speaker_entity_id: str = ""
    effects_sound_url: str = ""
    effects_max_volume: float = Field(default=0.30, ge=0, le=0.30)
    effects_max_duration_seconds: float = Field(default=4, gt=0, le=10)
    effects_quiet_hours: str = "22:00-09:00"
    effects_timezone: str = "America/New_York"

    feature_enabled: bool = False
    conversation_mode: Literal["greeting_only", "interactive"] = "greeting_only"
    openai_api_key: SecretStr = SecretStr("")
    openai_api_key_file: str = ""
    vision_model: str = ""
    dialogue_model: str = ""
    stt_model: str = ""
    tts_model: str = ""
    moderation_model: str = ""
    tts_voice: str = "alloy"
    personality: Literal["haunted_butler", "friendly_witch", "silly_skeleton"] = "haunted_butler"
    snapshot_count: int = Field(default=3, ge=1, le=3)
    snapshot_interval_seconds: float = Field(default=0.2, ge=0, le=1)
    vad_aggressiveness: int = Field(default=2, ge=0, le=3)
    vad_onset_ms: int = Field(default=210, ge=30, le=1000)
    vad_silence_ms: int = Field(default=720, ge=30, le=2000)
    max_visitor_replies: int = Field(default=3, ge=1, le=3)
    listen_timeout_seconds: float = Field(default=8, gt=0, le=8)
    max_session_seconds: float = Field(default=45, gt=0, le=120)
    provider_timeout_seconds: float = Field(default=10, gt=0, le=30)
    playback_timeout_seconds: float = Field(default=15, gt=0, le=30)
    cooldown_seconds: float = Field(default=30, ge=0)
    porch_clear_seconds: float = Field(default=10, ge=0)
    audio_echo_guard_ms: int = Field(default=400, ge=0, le=5000)
    fallback_wav: str = ""
    goodbye_wav: str = ""

    # Explicit _FILE support works with Compose secrets and local private files.
    unifi_username_file: str = ""
    unifi_password_file: str = ""
    unifi_api_key_file: str = ""
    unifi_audio_stream_url_file: str = ""
    control_api_token_file: str = ""

    @model_validator(mode="after")
    def load_secret_files(self):
        for name in ("unifi_username", "unifi_password", "unifi_api_key",
                     "unifi_audio_stream_url", "control_api_token", "openai_api_key",
                     "unifi_webhook_token", "home_assistant_token"):
            location = getattr(self, name + "_file")
            if not location:
                continue
            if getattr(self, name).get_secret_value():
                raise ValueError(f"Set only {name.upper()} or {name.upper()}_FILE")
            try:
                with Path(location).open("rb") as source:
                    data = source.read(65537)
                if len(data) > 65536:
                    raise ValueError("Secret too large")
                value = data.decode("utf-8").rstrip("\r\n")
                if not value or "\x00" in value:
                    raise ValueError("Invalid secret")
            except (OSError, ValueError):
                raise ValueError(f"Cannot load nonempty UTF-8 secret for {name.upper()}") from None
            setattr(self, name, SecretStr(value))
        return self


    @model_validator(mode="after")
    def validate_webhook_and_effect_formats(self):
        import ipaddress
        import re
        from urllib.parse import urlsplit
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        for value in self.unifi_webhook_allowed_source_ips.split(','):
            if value.strip():
                try:
                    ipaddress.ip_network(value.strip(), strict=False)
                except ValueError:
                    raise ValueError('Invalid webhook source network') from None
        if not re.fullmatch(r'[A-Za-z0-9-]+', self.unifi_webhook_token_header):
            raise ValueError('Invalid webhook header name')
        if self.effects_quiet_hours and not re.fullmatch(r'([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]', self.effects_quiet_hours):
            raise ValueError('Quiet hours must be HH:MM-HH:MM')
        try:
            ZoneInfo(self.effects_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Invalid effects timezone') from None
        for value, domain in ((self.hue_arrival_scene, 'scene'), (self.effects_speaker_entity_id, 'media_player')):
            if value and not re.fullmatch(domain + r'\.[a-z0-9_]+', value):
                raise ValueError('Invalid effects entity')
        if any(not re.fullmatch(r'light\.[a-z0-9_]+', e.strip()) for e in self.hue_controlled_entities.split(',') if e.strip()):
            raise ValueError('Controlled entities must be explicit light entity IDs')
        for value in (self.home_assistant_url, self.effects_sound_url):
            if value:
                parsed = urlsplit(value)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError('Effects URLs must be HTTP(S) without embedded credentials')
        return self
