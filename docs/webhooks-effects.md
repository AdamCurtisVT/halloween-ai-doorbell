# Alarm Manager webhooks and Home Assistant effects

The revised spec is implemented on the same unverified hardware assumptions as the existing conversation engine. The app owns orchestration; HA only controls designated lights and an optional dedicated effects player. No Hue SDK or second HA conversation automation is deployed.

## Network surfaces

`python -m app.server` runs two FastAPI listeners sharing one runtime/inbox:

- **8000:** management only. Compose publishes `127.0.0.1:8000:8000`. All controls except liveness require the control bearer token.
- **8080:** only `POST /webhooks/unifi`, with no management routes or docs. Set `DOCKER_LAN_IP` to the Docker host's specific LAN IP and allow inbound TCP 8080 only from Protect's console (or an explicitly trusted LAN proxy). Blank/unconfigured values bind to loopback so setup cannot accidentally expose the receiver globally.

Forwarding headers are ignored; Uvicorn proxy-header handling and access logs are disabled. Source allowlisting uses the actual socket peer. Docker Desktop/NAT or a proxy may change the observed peer: verify it with the diagnostic and keep a firewall allowlist at the true LAN boundary. Do not expand the allowlist to an entire LAN merely to bypass a mismatch. No internet exposure, query-token endpoint or unsigned-body trust is assumed.

## Configure Protect

In Protect Alarm Manager, select the doorbell and Person Detection, create a named alarm and configure an HTTP **POST** custom webhook to `http://<DOCKER_LAN_IP>:8080/webhooks/unifi`. The [Ubiquiti guide](https://help.ui.com/hc/en-us/articles/25478744592023-Send-UniFi-Protect-Alerts-to-Web-Services-using-Webhooks) documents the POST envelope below; it does not establish that every deployed version can send custom headers with POST. Confirm that on your version.

Configure a high-entropy `UNIFI_WEBHOOK_TOKEN` (at least 32 characters; `_FILE` supported) in `X-Unifi-Token`, or your configured `UNIFI_WEBHOOK_TOKEN_HEADER`. If POST headers are unavailable, use a **LAN-only reverse proxy** with a secret path, Protect-only source ACL, access logging disabled for that path, and inject the token header upstream. Store its secret privately. Allowlist the proxy's socket IP in the app and the Protect console at the proxy/firewall. The app does not trust `X-Forwarded-For` and does not expose a secret URL path itself. Do not assume the body is signed.

POST shape supported by the normalizer (values below are illustrative, not certified trigger/device values):

```json
{
  "alarm": {
    "name": "porch alarm",
    "sources": [],
    "conditions": [],
    "triggers": [{"key": "<observed-person-trigger-key>", "device": "<observed-device-identifier>"}]
  },
  "timestamp": 1793200000000
}
```

The timestamp must be an integer Unix time in **milliseconds**, within the default 120-second skew. Populate exact comma-separated allowlists:

- `UNIFI_WEBHOOK_ALLOWED_SOURCE_IPS`: console/proxy socket IP or narrow CIDR.
- `UNIFI_WEBHOOK_ALLOWED_CAMERA_IDS`: the deployed payload's device identifier (may be a MAC, not Protect's camera UUID).
- `UNIFI_WEBHOOK_ALLOWED_ALARM_NAMES`: exact configured alarm names.
- `UNIFI_WEBHOOK_ALLOWED_TRIGGERS`: exact keys verified to represent Person Detection; optionally verified doorbell-press keys.

All allowed identifiers map to the single `UNIFI_CAMERA_ID` used for snapshots/talkback. Never list devices other than that same physical doorbell. The app validates the complete trigger list; out-of-scope mixed triggers reject the envelope. Unsupported firmware payload shapes require an explicit normalizer change with a redacted fixture, not guessed field aliases. No unique event ID or presence/absence field is required or invented.

Auth/source checks precede body parsing. Body limit is 16 KiB by default, streamed with a three-second read timeout. Validated events are deduped by normalized camera/alarm/trigger/10-second bucket and enqueued with `202`; duplicates also get `202` without another queue item. Disabled/unavailable runtime returns `503`; full inbox returns `429` without consuming its idempotency key. Invalid/out-of-scope input returns `400`, unauthorized `401`, disallowed source `403`, oversized input `413`. No raw payload/credentials are logged.

`TRIGGER_SOURCE=webhook` is primary/default. `websocket` explicitly selects the old source; `webhook_with_websocket_fallback` enables both. Both use the same single-session/cooldown/inactivity admission, preventing concurrent duplicate effects/greetings. Detection end is **not** departure. Without additional verified presence data, silence/deadline/explicit cancellation close the session and a configured quiet interval plus cooldown rearms it. This is an inactivity heuristic, not occupancy detection.

## Arrival and restoration ownership

The implementation chooses the spec's **direct HA REST** alternative. Do not also run arrival/end HA scripts on these lights. Provide `HOME_ASSISTANT_URL`, a long-lived `HOME_ASSISTANT_TOKEN` (or `_FILE`), and explicit `HUE_CONTROLLED_ENTITIES` containing only designated `light.*` entities. Missing HA configuration or HA errors increment degradation counters and leave the doorbell conversation working. HA is external to Compose.

On approach, a bounded background task reads the prior state of all designated lights before changing any. It applies purple/orange at a one-second transition with brightness at least 160/255 (or preserves brighter settings). If `HUE_ARRIVAL_SCENE` is configured, confirm that the imported Hue scene only affects these porch lights and set `HUE_SCENE_SCOPE_CONFIRMED=true`; otherwise the adapter refuses it. Scene membership cannot be inferred safely for every Hue integration version. Prefer a verified imported scene for coordinated porch groups.

The task records a session lease containing before/owned light states. After a transition, changed HA context is treated as ambiguous/manual ownership. Cleanup runs in `finally`, with an independent timeout. Each restore compares current state, attributes, `last_updated` and context to the session-owned values; modified lights are skipped individually. On-state restore uses the saved brightness and one compatible saved color representation. Originally-off lights may return to their prior off state only under that same ownership check; no failure fallback blindly turns lights off.

**REST does not offer an atomic compare-and-restore transaction.** A change between the final GET and service POST cannot be ruled out, and integration-driven attribute changes may conservatively skip restoration. Validate Hue attribute round-tripping and transition context on your installed version. For stronger ownership guarantees move the whole effect/lease/restore contract into a verified HA script/helper implementation; do not mix both styles for the same lights.

No HA temporary snapshot scenes are used. HA restart/context loss skips stale restoration and leaves the safe arrival state. App restart loses in-memory leases; it likewise does not force old state over current lighting. Configure a static porch baseline scene in HA, with safe stair brightness and no flashing/off actions, for **manual/operator recovery** after outages. Automatically applying a fallback scene without confirmed ownership could overwrite newer user changes, so this app does not do that.

## Independent stinger policy

Stingers are off by default. To opt in, set `EFFECTS_SPEAKER_ENABLED=true`, `EFFECTS_SPEAKER_DEDICATED=true`, an explicit `media_player.*` entity and an owned/licensed `EFFECTS_SOUND_URL` reachable over HTTP(S) by HA/the player. Docker-local paths are rejected. Keep assets at or below `EFFECTS_MAX_DURATION_SECONDS` (default four seconds). A service request is not proof the remote asset played; verify actual playback/stop timing.

An already playing/paused household media session is not interrupted. On the dedicated idle player, the adapter caps volume at 0.30 and never raises a lower existing volume. Quiet hours default to `22:00-09:00`, using `EFFECTS_TIMEZONE=America/New_York`; they suppress audio without suppressing arrival lighting. `EFFECTS_ENABLED=false` leaves the existing lighting baseline untouched; the speaker has its own switch. Maintain safe baseline path lighting independently of optional Halloween effects.

The stinger starts during observation and has an auto-stop task. Before doorbell speech, the controller waits only for the bounded approach task, requests stop and confirms the player is idle/off/standby/paused. Volume is restored only if it still matches the effect-owned setting. A newer different media URL is not stopped; its presence makes input unsafe. If stop/idle cannot be confirmed, the conversation degrades to a greeting without opening STT. Doorbell playback still has its echo guard. A failed network stop cannot physically enforce a remote player's duration; the short asset and dedicated player are required deployment controls.

## Diagnostics and evidence

1. With the regular app stopped on 8080, run `python -m scripts.check_webhook --bind <LAN-IP> --seconds 60`. It uses the production auth/size/source/normalization path but only prints sanitized events and hashed socket-source evidence. It never starts AI/HA/audio. Trigger a real Person Detection POST; preserve private raw payloads only long enough to configure/test mapping, then redact/delete them. Stop the diagnostic before starting the shared server.
2. `python -m scripts.check_effects` reads configured HA light/scene state without changing it. Add `--approve-effects` for one controlled arrival/stinger/stop/conditional-restore test. An observer must confirm actual lights, volume, restoration and manual-change preservation.
3. Start the app disabled, run preflight, explicitly enable, and test webhook arrival before inference, dedupe across sources, HA loss, manual changes, HA restart, quiet hours and speaker bleed into the microphone. No AI work or HA calls happen inline with webhook acknowledgement.
4. Complete the [compatibility report](compatibility-report-template.md) and [acceptance runbook](acceptance.md). Reported `hardware_verified` remains false; no hardware/HA test has been performed by these code changes.

Sources verified for this implementation: [Protect webhook envelope/setup](https://help.ui.com/hc/en-us/articles/25478744592023-Send-UniFi-Protect-Alerts-to-Web-Services-using-Webhooks), [HA REST state/service APIs](https://contributors.home-assistant.io/core/internals/rest-api/), [HA scene behavior](https://www.home-assistant.io/docs/scene/). No custom Hue API paths or undocumented HA paths are embedded.
