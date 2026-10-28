# Runtime setup and operations

The user authorized continued implementation on assumed hardware compatibility. This does not certify hardware behavior. `hardware_verified` stays false in status/readiness, and the compatibility report remains unfilled. Software readiness reports connectivity/configuration, not proven acoustic performance.

## Activate deliberately

1. Run tests and `python -m scripts.simulate` without credentials.
2. Prepare and review the fallback and goodbye WAVs (`python -m scripts.prepare_audio` on macOS); both must be complete mono/stereo PCM WAVs of at most 10 seconds and 2 MB, with an 8-192 kHz sample rate.
3. Set Protect local credentials, API key, camera ID and trusted TLS. Configure person detection/porch zones in Protect.
4. Set `OPENAI_API_KEY` (or `_FILE`) and a random `CONTROL_API_TOKEN`. Choose `VISION_MODEL`, `DIALOGUE_MODEL`, `TTS_MODEL` and `MODERATION_MODEL` from current official OpenAI documentation and your account availability. Vision needs image input plus structured Responses output (one call returns the costume and the opening greeting); dialogue needs structured Responses output; moderation needs text classification; speech needs raw PCM output, which the app wraps into a WAV. There are deliberately no implicit model IDs. `TTS_VOICE=alloy` is configurable. SDK retries are disabled so the controller owns deadlines.
5. Set `FALLBACK_WAV` and `GOODBYE_WAV` to paths readable by the running process. For Docker use `/media/fallback.wav` and `/media/goodbye.wav` with the optional media mount file below. Optionally set `ARRIVAL_WAV` to a short in-character line played the moment a visitor arrives, covering the seconds it takes to generate the costume greeting.
6. Initially select `CONVERSATION_MODE=greeting_only`. Set `FEATURE_ENABLED=true` only when you intend to connect, send snapshots to the provider and play audio on person events. Missing required configuration or invalid fallback files fails startup.
7. For interactive mode additionally configure `STT_MODEL` for file transcription and a supported `UNIFI_AUDIO_STREAM_URL`. Choose `CONVERSATION_MODE=interactive`. The live reader begins consuming/discarding audio immediately on connection; only VAD-selected speech during an active listening turn is submitted for transcription.

```sh
# Basic env-file deployment with reviewed local WAVs mounted read-only.
docker compose -f docker-compose.yml -f docker-compose.runtime.yml up --build -d
# Or the explicit file-secret variant:
docker compose -f docker-compose.secrets.yml \
  -f docker-compose.ha-secret.yml -f docker-compose.microphone-secret.yml \
  -f docker-compose.runtime.yml up --build -d
```

No model/provider request has been tested against an account. Unsupported model capabilities, permissions, refusals, bad output and latency cause stage failures/fallbacks. Validate account access and real output independently. Review provider audio/data retention separately; local deletion and Responses `store=false` are not promises of zero provider retention.

## Session behavior

A validated selected-camera Alarm Manager webhook starts one session. Webhook deduplication uses camera/alarm/trigger/time bucket because no unique event ID is assumed. The bounded inbox is consumed asynchronously; HA and AI never run inline with acknowledgement. A shared manager suppresses both webhook and optional WebSocket duplicates during a visit. Rearming requires cooldown plus an inactivity interval, which is an explicit heuristic, **not proof of physical absence**. Detection-end updates are ignored as departure signals. Silence, the deadline or explicit cancel closes a session; the app does not invent a `person_left` webhook. Reliable physical-departure detection would require a separately verified source.

The service captures up to three JPEGs over a short window (configurable count/interval) and selects a frame using a bounded exposure/contrast heuristic; this is not a guarantee of recognition quality. One vision call returns both the costume description and the opening greeting. `VISION_DETAIL` defaults to `high` because visitors are small in a doorbell's wide view. If no frame is available or the vision call fails, a plain dialogue greeting is generated for an unknown costume; if that also fails, the cached fallback WAV plays. Dialogue is at most 25 words and one question, uses fixed child-friendly instructions, validates basic unsafe/PII patterns and runs output moderation. Image text, costume output and visitor history are untrusted input; no model tools or external actions are available. These checks reduce risk but still require real-world content testing, especially with children, groups and poor images.

Input is closed and cleared before synthesis/playback, stays closed through a configurable echo tail, then opens only if the microphone is healthy. The reader continuously drains FFmpeg, with a maximum 100-frame queue (~3 seconds). WebRTC VAD uses 30 ms frames, ~210 ms consecutive voiced onset and ~720 ms trailing silence. These conservative defaults require tuning outdoors. `LISTEN_TIMEOUT_SECONDS` bounds the wait for speech to start; once it starts, the utterance runs until trailing silence or eight seconds, so a visitor who answers late is not cut off. No speech, unintelligible transcript and stream failure have distinct counters.

The controller enforces at most three visitor replies independent of model output. A final or model-ended reply is played before a cached goodbye. A visitor's explicit goodbye also plays the cached goodbye. A new listen/reply turn starts only if `MIN_TURN_SECONDS` remain after holding back `GOODBYE_RESERVE_SECONDS`; otherwise the goodbye plays. A turn still running when its time is up is cut off and the goodbye plays, so the session deadline does not end a visit in silence. Silence, output error or cancellation may terminate without a goodbye; the hard deadline remains as a backstop. Generation/STT failures and unhealthy input stop the dialogue or use cached safe audio. No fourth listening turn is opened.

All generated speech files live in a temporary session directory and are deleted on teardown. Input PCM, costume context and history stay in memory; no visitor transcript/image/audio persistence is implemented. Temporary files on a host disk can survive forced process death; Docker's tmpfs limits this exposure. Approved fallback assets are intentionally retained. There are no debug retention switches.

## Configuration limits

| Setting | Default | Behavior |
| --- | --- | --- |
| FEATURE_ENABLED | false | No hardware/provider activity until true |
| CONVERSATION_MODE | greeting_only | interactive opts into mic/STT |
| MAX_VISITOR_REPLIES | 3 | Valid range 1–3 |
| LISTEN_TIMEOUT_SECONDS | 8 | Wait for speech to start; positive, maximum 8 |
| MAX_SESSION_SECONDS | 45 | Overall deadline; configurable up to 120 |
| MIN_TURN_SECONDS | 12 | Time needed to start another reply turn |
| GOODBYE_RESERVE_SECONDS | 6 | Held back for the goodbye; must exceed its length by 1 s |
| PROVIDER_TIMEOUT_SECONDS | 10 | Per-stage bound, maximum 30 |
| PLAYBACK_TIMEOUT_SECONDS | 20 | Playback bound, maximum 30; generated speech can last 15 s |
| VISION_DETAIL | high | Image detail for the vision call: high, low or auto |
| ARRIVAL_WAV | blank | Optional cached line played on arrival |
| AUDIO_ECHO_GUARD_MS | 400 | Input closed after playback |
| COOLDOWN_SECONDS | 30 | Minimum post-session suppression |
| PORCH_CLEAR_SECONDS | 10 | Quiet interval after tracked events end |

Protect and microphone reconnection use backoff capped at 30 seconds. There is no unbounded immediate restart loop. A reconnect discards trigger continuity and requires fresh quiet time before admitting a new arrival. Microphone interruption aborts the current listening turn; later visits can try again after recovery. Incoming and outgoing media are independent adapters; a healthy speaker does not imply a healthy microphone.

## Local API

All endpoints except `/health` require `Authorization: Bearer <CONTROL_API_TOKEN>`. Keep the API on loopback. Credentials belong in an HTTP client secret facility, not pasted into shared shell history/logs.

- `GET /health`: process liveness only.
- `GET /status`: enabled/mode, state, connectivity, microphone health, safe error category, the 20 most recent failures (stage, exception type and HTTP status, time), cumulative counters/timing totals and reconnect counts. Never returns media, transcripts, IDs or credentials.
- `GET /ready`: 503 while disabled/disconnected, or while interactive input is degraded; 200 for software readiness. Hardware verification remains false.
- `POST /cancel`: cancels current work and applies cooldown; service stays enabled for future eligible visits.
- `POST /disable`: cancels sessions and stops the supervisor/microphone/provider connections. Use authenticated `/enable` with explicit camera/audio approval to enable again, or restart with `FEATURE_ENABLED=true`.

Generated logs contain only an ephemeral session ID and state, or for failures the stage (for example `greeting`, `tts`, `playback`, `effects_restore`), the exception type and any HTTP status. Exception messages are never logged because they can contain URLs, credentials or transcripts. Provider/Protect log levels are suppressed; do not enable SDK debug logging or HTTP body tracing with real visitors/secrets. Metrics are in memory and reset on process restart. They include stage timing totals, started/completed/failed/cancelled sessions, playback/fallback counts, visitor replies, silence/unintelligible outcomes, duplicate/suppressed events, queue drops and reconnects. They are not persisted or exported to a metrics backend.

## Remaining validation

Hardware diagnostics and the compatibility report still need to be completed. Test a real arrival → single greeting, controlled three-turn exchange, darkness/groups/no reply, quiet/background speech, webhook source/scoping and inactivity timing, echoed audio and actual playback completion. Exercise Protect/network loss, STT/LLM/TTS errors and recovery on the target host. Confirm temp media deletion, acceptable provider retention, visitor disclosure/consent and outdoor volume before unattended use. No reliability or child-safety field acceptance is claimed by the fake tests.


## Completion additions

`GET /preflight` is an authenticated offline configuration/media/tool check. `POST /enable` starts the configured runtime without requiring a service restart; it is idempotent. `POST /test/session` admits a real end-to-end test through the same single-session/cooldown path, without fabricating a Protect event. Both POSTs require JSON `{"approve_camera_audio": true}`. A test is rejected with 409 while disabled, disconnected, within the inactivity interval, busy or cooling down. It can capture media, upload to configured providers and play real audio. Disabling stops these resources; no separate test playback pipeline bypasses the session controller.

WebSocket-only readiness requires a connected event subscription as well as Protect login. Primary-webhook readiness instead requires the shared LAN listener to be running; it does not require a WebSocket connection. A catalog-access preflight does not prove that a model supports the selected image/schema/audio operations. Run the complete [acceptance sequence](acceptance.md) when integration testing begins.

`PERSONALITY` supports `haunted_butler`, `friendly_witch` and `silly_skeleton`; all use the same safety rules. Arbitrary replacement system prompts are not supported. `SNAPSHOT_COUNT` is 1–3, `SNAPSHOT_INTERVAL_SECONDS` is 0–1. `VAD_AGGRESSIVENESS` is 0–3, `VAD_ONSET_MS` is 30–1000 and `VAD_SILENCE_MS` is 30–2000. Durations round up to complete 30 ms frames. Defaults remain 210 ms onset / 720 ms silence.

The [webhook/effects guide](webhooks-effects.md) covers the new primary trigger, two-listener deployment, HA REST ownership, quiet hours, audio gating, lease checks and no-HA degradation. Start `python -m app.server`, rather than standalone `uvicorn app.main:app`, to serve both surfaces.

For file-secret deployment, the HA and microphone override files are optional: omit them and their secret files for greeting-only/no-HA operation. Blank quiet hours now work in both Compose variants. On process shutdown activation is permanently blocked, active media is cancelled before HTTP draining, and Compose allows 60 seconds for cleanup. A normal `/disable` remains reversible through `/enable`.
