# Halloween AI Doorbell — Engineering Handoff

**Status:** Design / unimplemented; revised for webhook + Home Assistant effects  
**Target hardware:** Ubiquiti UniFi G4 Doorbell Pro with UniFi Protect  
**Deployment:** Self-hosted Docker Compose; Python 3.12  
**Mode:** Interactive, half-duplex; up to three visitor replies  
**Automation:** UniFi Protect Alarm Manager HTTP POST webhook primary; WebSocket fallback  
**Effects:** Home Assistant controls Philips Hue scenes and optional effects speaker  
**Date:** 2026-10-28

## 1. Executive summary

Build a self-hosted Halloween porch experience. On a qualifying UniFi Protect Alarm Manager person-detection HTTP POST webhook, capture a camera image, identify or describe a possible Halloween costume with a vision-capable model, generate a short family-friendly character greeting, and play it through the G4 Doorbell Pro speaker. Then listen for the visitor's response via the doorbell microphone, transcribe it, and reply in character. Support at most three visitor replies per session, with strict timeouts and a greeting-only fallback. Immediately on approach, cue Hue porch lighting through Home Assistant and optionally play a short sound effect through a separate speaker; coordinate these effects with doorbell speech, and restore the original lighting at session end.

**Implementation recommendation:** Use this document as `SPEC.md` in a Git repository and hand it to Codex for phased implementation. Do not assume unverified UniFi microphone/audio APIs work; validate them before writing the complete application.

## 2. Goals and non-goals

### Goals
- Initiate automatically on a qualified person event; optional doorbell-press trigger.
- Capture one or more appropriate frames; use AI to describe a costume or acknowledge uncertainty.
- Produce short, amusing, age-appropriate responses with a configurable character persona.
- Play speech on the existing doorbell speaker where supported.
- Capture visitor speech from an authenticated camera audio stream if available.
- Enforce half-duplex speaking/listening and up to 3 visitor replies.
- Avoid duplicate greetings for one visit; recover from disconnected services.
- Run as a maintainable, testable Docker Compose workload, using existing Home Assistant for optional lighting and effects-speaker control (with a no-HA degradation path).
- Trigger responsive arrival lighting immediately after verified webhook receipt, without waiting for AI.
- Preserve and restore pre-visit light state without disrupting user changes.

### Non-goals (v1)
- Identifying visitors by face, name, voiceprint, or biometric recognition.
- Always-on cloud streaming or continuous recording/retention.
- Full-duplex or interruption/barge-in handling.
- Storing visitor photos, audio, or transcripts long-term.
- Public web dashboard, multi-camera distribution, or Kubernetes.
- Animatronic control; arbitrary full-house lighting changes. A configurable external effects speaker **is** in scope, but it is separate from the doorbell conversation output.

## 3. Reference architecture

```text
G4 Doorbell Pro / UniFi Protect Alarm Manager
       |  person detection HTTP POST webhook (primary)
       v
FastAPI POST /webhooks/unifi  ---> Validate auth/source/camera/alarm
       |                              Dedupe and bounded queue
       +----> Immediate arrival effects (non-blocking / timeout bounded)
       |              |
       |              v
       |         HomeAssistantAdapter ----> Philips Hue scene / color / brightness
       |              |                    Optional media_player sound effect
       |              v
       |         Lighting snapshot + effect lease / restore
       v
SessionController (one active porch session)
       |-> ProtectSnapshotAdapter -> VisionAdapter -> ConversationAdapter
       |-> Incoming RTSPS/FFmpeg -> VAD -> STT (while LISTENING)
       |-> TTS -> Protect talkback (while SPEAKING)
       |-> Optional effects cues -> HA (not overlapping with STT by default)
       +-> Closing/cooldown -> HA restore / release

UniFi Protect WebSocket listener: optional backup source; avoid double triggering.
Home Assistant: existing external service, not deployed inside this Compose project.
```

### Ownership and boundaries
- **Docker app owns:** webhook authentication, event normalization, queueing/deduplication, session state, AI, microphones, doorbell talkback, effect timing, and lifecycle. It is the orchestration authority.
- **UniFi Protect owns:** person detection, Alarm Manager webhook delivery, camera snapshots, authenticated input stream, doorbell talkback.
- **Home Assistant owns:** Hue integration, lights/groups/scenes, optional effects speaker/media_player. Python calls HA via authenticated REST API or an HA script; HA does not independently start a second conversation on the same event.
- **Hue Bridge:** controlled via the existing HA Hue integration, not by a second Hue SDK in Docker.
- **Sound-effects speaker:** an HA `media_player`, independent of doorbell conversation output. Doorbell talkback remains the primary spoken-dialogue endpoint until separately reconfigured.
- **Fallback:** when HA is unavailable, log effects degradation and continue the doorbell conversation. HA failure must never block webhook acknowledgment or AI processing.

### Home Assistant action contract
Define an `EffectsAdapter` interface with `on_approach(session_id)`, `on_greeting(session_id, costume)`, `on_reply(session_id, sentiment?)`, and `on_end(session_id, outcome)`. Initial implementation: `HomeAssistantEffectsAdapter`; testing: `FakeEffectsAdapter`; optional `NoOpEffectsAdapter`. Keep effects idempotent by session ID.

Prefer two HA scripts (`script.halloween_arrival_effect` and `script.halloween_end_effect`) plus optionally `script.halloween_play_stinger` so HA holds device-specific entity IDs, scene names, and sound playback details. Invoke `POST /api/services/script/turn_on` with bearer authorization and a JSON `entity_id` + variables; validate actual script invocation payload/API on installed HA version. Alternatively call `scene.turn_on`, `light.turn_on`, and `media_player.play_media` directly from the adapter. Do not mix ownership styles for the same lights.

Hue scenes imported through HA should be preferred for coordinated groups. Ensure the configured group/scene only affects designated porch lights. Default arrival visual: spooky purple/orange, 0.5–1.5 s transition, sufficient brightness for safe stairs and camera identification. Avoid high-frequency flashing/strobing and do not abruptly extinguish path lighting. Make volume, quiet hours, and audio playback optional and configurable.

### Webhook security and networking
- In Protect **Alarm Manager**, configure an alarm scoped to the doorbell with **Person Detection** and an HTTP **POST** custom webhook; doorbell press can be a second trigger using the same normalization pipeline.
- Endpoint: `POST /webhooks/unifi` on a **LAN-reachable** Docker host address. The prior `127.0.0.1:8080:8080` Compose mapping must change to a LAN-bound interface, e.g. `${DOCKER_LAN_IP}:8080:8080`, with firewall allowlisting to Protect's console. Do not publish to the internet.
- Configure a high-entropy token in a custom header if supported by the deployed Protect UI (or a secret URL path behind a LAN-only reverse proxy). Check the token with constant-time comparison; authenticate before parsing large payloads. The webhook body itself should not be assumed cryptographically signed.
- Restrict inbound source IPs, request body size, accepted camera IDs, alarm names/triggers, and timestamp skew. Redact payloads and credentials in logs.
- Return `202 Accepted` promptly after validation and bounded enqueue; do not run AI, audio playback, or HA action inline with the request. Reject full queues with a controlled status.
- Do not assume the example POST payload has a unique event ID or definite presence/absence state. Use normalized `(camera, alarm, trigger, time bucket)` for idempotency and session cooldown. **Person detection does not prove a visitor is still present or has departed**; use additional camera state/polling or inactivity heuristics, not a fictional `person_left` webhook.

## 4. UniFi hardware/API validation — mandatory first milestone

UniFi Protect firmware/API behavior can differ by controller and camera version. Before implementing conversational AI, produce a reproducible hardware-compatibility report containing:

1. Protect version, camera firmware, G4 Doorbell Pro model/variant, relevant network topology (without publishing secrets).
2. A successful **Alarm Manager POST webhook** from a real person event, with redacted payload, custom-header support result, source-IP proof, trigger normalization and deduplication; optional WebSocket fallback verified separately.
3. A successfully captured JPEG from the same camera.
4. A successful short WAV or compatible audio playback on the G4 Doorbell Pro speaker; report exact API/library version and audio format.
5. A successful **incoming microphone recording** from an authenticated stream. Verify audio track present using `ffprobe`, decode it with FFmpeg and transcribe a controlled test phrase.
6. An explicit report if the audio input and talkback paths interfere or audio feedback occurs.
7. Verify Home Assistant API reachability and Hue scene/entity IDs; run one arrival effect and a controlled restore without altering unrelated lights.
8. If a sound-effects speaker is configured, verify playback, URL reachability, volume behavior, and whether microphone captures the effect; test sound effect before opening the listen gate.

Do not conflate the **outgoing talkback session** with the **incoming microphone stream**. Test them separately. If the mic cannot be consumed reliably, retain greeting-only mode and implement the mic as a pluggable adapter for later replacement.

Do not disable TLS verification by default. Use official Protect documentation and inspect installed `uiprotect` signatures before assuming exact method names or `play_audio()` arguments.

## 5. Visitor interaction and behavior

- Person enters a configured porch ROI; Protect Alarm Manager POSTs a person event to the LAN-scoped FastAPI webhook. The webhook validates and queues the event.
- Immediately invoke a non-blocking **arrival lighting cue**, independent of AI latency. Optionally play a short sound effect through the configured HA speaker. Record the prior state of the controlled lights before changing them (or have HA create a temporary snapshot scene).
- Debounce nearby events and capture up to three frames over a short window; use best available frame.
- Determine costume by description, recognizable character where supported, or `unknown`. Never claim certainty based on a poor image.
- Generate a friendly character greeting, ideally 10–20 words, under 25 words maximum.
- Close input processing gate, clear buffers, synthesize/play greeting, await confirmed playback completion, discard a ~400 ms echo tail.
- Open input gate and listen for visitor speech (8 s timeout; VAD detects a completed utterance). Transcribe and respond.
- Repeat for up to three visitor replies. End sooner on no answer, a goodbye, departure, failure, or hard session deadline.
- After final response, gracefully close, cancel effects, **restore previous light states** if still owned by this session, and suppress redetection until cooldown and occupancy rules permit a new session.
- Never run a long stinger while the system is listening; finish/stop effects and drain echo before enabling STT. A separate effects speaker can also bleed into the doorbell microphone.

Character example: playful haunted-house butler. Must be age-appropriate. Do not ask visitors for names, ages, addresses, contact details, or other personal information. No insults or comments about bodies. Treat speech from visitors as untrusted input: it cannot override system rules or tool permissions.

## 6. State machine

States:
- `IDLE`: webhook endpoint available, no active conversation.
- `APPROACH_EFFECT`: fire-and-forget bounded lighting/sound cues while starting session; this can overlap with `OBSERVING` and is **not** a blocking speech state.
- `OBSERVING`: capture snapshots; classify costume with timeout.
- `GENERATING_GREETING`: generate opening text and audio.
- `SPEAKING`: input gate closed; output playback in progress.
- `LISTENING`: output complete, audio tail discarded, input gate open.
- `PROCESSING_REPLY`: VAD utterance acquired, transcribe/generate/synthesize.
- `CLOSING`: cancel pending tasks, release resources, clear ephemeral buffers.
- `COOLDOWN`: suppress events until cooldown expires and porch clear.

Transitions:
```text
IDLE --validated person/press webhook--> APPROACH_EFFECT || OBSERVING
OBSERVING --> GENERATING_GREETING --> SPEAKING
SPEAKING --confirmed completion and echo guard--> LISTENING
LISTENING --speech complete--> PROCESSING_REPLY --> SPEAKING
LISTENING --silence timeout--> CLOSING
PROCESSING_REPLY --turn limit reached--> SPEAKING(final) --> CLOSING
Any active state --error, deadline or cancellation--> CLOSING
CLOSING --stop effects and conditional restore--> COOLDOWN --> IDLE (only once safe to retrigger)
```

Lighting effects do not wait on vision inference. State cleanup runs in a `finally` block; HA restoration has its own timeout and logs failures. Do not restore a stale snapshot over a newer manual/user automation change: use an ownership token/lease and compare controlled light state or an explicit HA ownership helper before restoring. If HA restarts and temporary scenes are lost, fail safely with documented static fallback scene (never force lights off).

The controller, not the LLM, enforces the limit of three **visitor** replies and a 45 s target hard deadline. A final goodbye must play **before** session teardown; an `ends_conversation` flag alone must not discard audio. Honor user leaving and emergency cancellation promptly. Process at most one session concurrently per camera, with an `asyncio.Lock` and a bounded event queue.

## 7. Audio details

### Incoming
- Obtain authenticated stream with microphone audio enabled, if Protect permits it on this device/firmware.
- Use FFmpeg to decode the audio track to 16 kHz mono signed 16-bit PCM. Avoid piping credentials to logs.
- Consume audio continuously, including during playback; discard input frames while gate is closed to prevent pipe/backpressure and self-transcription.
- Use a small bounded ring buffer; clear it upon entering listening.
- VAD baseline: 20–30 ms frames, ~200 ms speech onset, ~700 ms trailing silence; configurable and verified against the chosen VAD package.
- Limit each captured utterance to ~8 s. Distinguish no speech, unintelligible speech and hardware-stream failure.
- Transcribe completed utterances only. Avoid storing raw audio or transcripts unless an explicit debug setting is enabled.
- Reconnect FFmpeg on EOF/stall with bounded exponential backoff; surface microphone degraded state.

### Outgoing
- Generate short speech audio through a replaceable synthesis provider; validate output codec and sample rate requirements with talkback.
- `AudioOutputAdapter.play()` must have reliable completion semantics; if talkback is fire-and-forget, independently track enough playback duration to avoid reopening input prematurely.
- Close audio input gate before output; drain/clear stale buffers and wait an initial configurable ~400 ms tail after playback.
- On output error, do not reopen input; fail or use a separately configured output device.

### Sound effects / audio policy
- Effects speaker is a separate configurable HA media player, not the AI dialogue output by default.
- Short arrival stinger can play during observation, but enforce a maximum duration and stop it before dialogue playback or the first LISTENING state.
- Explicit duck/mute or stop on transitions; avoid unintended interruption of existing household media sessions (dedicated speaker preferred).
- Play only licensed or owned audio assets, hosted on a URL reachable by HA/player, or use built-in HA-supported media; do not assume a local Docker filesystem path is playable by a remote speaker.
- Configure volume ceiling, quiet hours, and on/off switches for stingers independently from voice; disabling effects must not disable safety lighting.

### Future compatibility
- Optional separate network speaker or microphone adapter may be added later.
- Do not add full-duplex realtime media until the turn-based system is stable.

## 8. AI provider contracts

### Vision output (illustrative)
```json
{
  "costume_detected": true,
  "costume_name": "Spider-Man",
  "description": "Red and blue superhero suit with a spider motif",
  "certainty": "likely",
  "people_count": 1
}
```
`certainty` is an interpretive category, not a calibrated probability. Handle `unknown`, poor lighting and groups gracefully. Never perform facial recognition.

### Conversation output
```json
{
  "response_text": "Even superheroes need candy. What's your favorite?",
  "tone": "playful",
  "ends_conversation": false
}
```
Validate schema and length, cap output to 25 words, filter inappropriate outputs, allow a maximum of one question per spoken turn. Prompt injection from visitor speech must not alter instructions.

### Fallbacks
- Vision timeout -> generic greeting.
- Dialogue timeout -> prerecorded safe line.
- Speech synthesis failure -> cached prerecorded audio.
- Microphone stream/STT failure -> greeting-only session, record degraded health.
- Talkback failure -> do not attempt an interactive conversation; log and release session.

## 9. Configuration defaults

```dotenv
UNIFI_HOST=
UNIFI_USERNAME=
UNIFI_PASSWORD=
UNIFI_CAMERA_ID=
UNIFI_AUDIO_STREAM_URL=
UNIFI_WEBHOOK_TOKEN=
UNIFI_WEBHOOK_ALLOWED_CAMERA_IDS=
UNIFI_WEBHOOK_ALLOWED_SOURCE_IPS=
DOCKER_LAN_IP=
HOME_ASSISTANT_URL=http://homeassistant.local:8123
HOME_ASSISTANT_TOKEN=
EFFECTS_ENABLED=true
HUE_ARRIVAL_SCENE=scene.halloween_porch
HUE_CONTROLLED_ENTITIES=light.porch_1,light.porch_2
HUE_RESTORE_MODE=snapshot_conditional
EFFECTS_SPEAKER_ENABLED=false
EFFECTS_SPEAKER_ENTITY_ID=media_player.porch_speaker
EFFECTS_SOUND_URL=
EFFECTS_MAX_VOLUME=0.30
EFFECTS_MAX_DURATION_SECONDS=4
EFFECTS_QUIET_HOURS=22:00-09:00
OPENAI_API_KEY=
VISION_MODEL=<choose_supported_model>
DIALOGUE_MODEL=<choose_supported_model>
STT_MODEL=<choose_supported_model>
TTS_MODEL=<choose_supported_model>
CONVERSATION_MODE=interactive
PERSONALITY=haunted_butler
MAX_VISITOR_REPLIES=3
LISTEN_TIMEOUT_SECONDS=8
MAX_SESSION_SECONDS=45
COOLDOWN_SECONDS=30
AUDIO_ECHO_GUARD_MS=400
STORE_IMAGES=false
STORE_AUDIO=false
STORE_TRANSCRIPTS=false
LOG_LEVEL=INFO
```

Treat `.env` and audio stream URL as secrets; do not commit. Prefer file-mounted secrets when deploying permanently. Verify that credentials have the narrowest suitable Protect permissions. Do not use undocumented settings as if they were official camera API fields.

## 10. Proposed repository

```text
halloween-ai-doorbell/
  SPEC.md
  README.md
  .gitignore
  .env.example
  Dockerfile
  docker-compose.yml
  pyproject.toml
  app/
    main.py
    config.py
    protect/{client,webhook,events,camera}.py
    effects/{base,home_assistant,no_op}.py
    audio/{input,vad,transcription,synthesis,output}.py
    ai/{vision,conversation,prompts}.py
    sessions/{models,state_machine,manager}.py
    api/{routes,schemas}.py
  scripts/
    check_protect.py
    test_talkback.py
    test_microphone.py
  tests/
    test_state_machine.py
    test_event_deduplication.py
    test_webhook_security.py
    test_effects_restore.py
    test_home_assistant_adapter.py
    test_audio_gate.py
    test_conversation_contracts.py
    test_failures.py
```

Dependencies: Python 3.12, FastAPI/Uvicorn, compatible pinned `uiprotect`, OpenAI SDK, Pydantic Settings, HTTPX, FFmpeg, VAD library, pytest/pytest-asyncio. Investigate exact current provider API calls before coding. Keep the container non-root, localhost-bind management endpoint, and restrict writable paths to tmpfs/cache. Protect test/config endpoints with authentication and do not expose them on the public internet.

## 11. Build sequence and acceptance gates

### Phase A — Hardware integration spike (first PR)
- Minimal Docker Compose, `ffmpeg`/`ffprobe` and basic connectivity diagnostics.
- Connect to Protect; discover camera ID; dump redacted capability report.
- Capture snapshot, configure Alarm Manager webhook and inspect a real event. Keep WebSocket listening as optional fallback only.
- Validate LAN routing/firewall and webhook token; confirm fast `202` and asynchronous queue.
- Confirm Home Assistant Hue integration, selected porch light/scene entities, and a test arrival cue plus conditional restore.
- Optionally validate media player sound effect delivery with mic echo guard.
- Play a short test phrase from an approved audio file.
- Capture 5–10 seconds of microphone audio and transcribe a known test phrase.
- Document what works, exact library/firmware versions and blockers.

**Gate:** Webhook trigger, HA Hue arrival/restore, and Protect snapshots are confirmed; both talkback and microphone are confirmed in an actual test before turning on interactive mode. Otherwise proceed with greeting-only mode while preserving audio interfaces.

### Phase B — Costume greeting MVP
- Add vision classification with structured outputs and uncertainty.
- Generate a bounded, family-friendly greeting.
- Synthesize speech and play it through verified output adapter.
- Add duplicate-event suppression, cooldown, safe fallbacks, logs.
- Trigger Hue lighting promptly on arrival independent of vision latency; optional sound-effect cue before speech.

**Gate:** A real arrival causes exactly one appropriate greeting with no manual step; Hue changes before the AI greeting and restores after the session.

### Phase C — Interactive conversation
- Continuous FFmpeg audio reader, bounded PCM buffers, VAD, STT.
- State machine, half-duplex gate, echo suppression, deadlines.
- Conversation history; max three visitor replies; goodbye-on-last-turn.
- Cancel/cleanup under exceptions, departure and timeouts.

**Gate:** Three-turn controlled exchange succeeds without the system transcribing its own audio or repeating greetings.

### Phase D — Reliability and privacy
- Repeat with darkness, groups, quiet speakers, background noise and no reply.
- Test Protect loss, HA unavailability, manual Hue changes, network loss, STT/LLM/TTS errors, stream restarts.
- Add `/health`, `/ready`, authenticated local status/test endpoints.
- Restrict logs; verify ephemeral media deletion; secure deployment.

**Gate:** Recover from failures without stuck sessions, unbounded memory, or unsupervised restart loops.

## 12. Test cases

1. Same person generates multiple Protect motion/person events -> only one greeting.
2. No clearly identified costume -> generic, non-embarrassing response.
3. Visitor says nothing -> clean timeout, no hallucinated transcript.
4. Visitor speaks while system is talking -> audio discarded; no self-response.
5. Visitor speaks after greeting -> one transcription and contextual answer.
6. Visitor gives three answers -> final spoken goodbye, no fourth listening turn.
7. Third answer arrives near deadline -> deterministic deadline behavior without hanging playback.
8. Person leaves mid-session -> close safely and suppress stale late replies.
9. Audio stream EOF -> mark degraded, retry without blocking event listener.
10. TTS/talkback unavailable -> no broken conversation session.
11. Group costumes or obscured image -> cautious classification and group-appropriate greeting.
12. Speech tries to override instructions -> remains within role and safety rules.
13. A person approaches -> Hue cue begins before inference completes.
14. Repeat webhook -> no duplicate Hue/stinger actions.
15. HA unreachable -> conversation proceeds and records effects degraded.
16. Visitor exits / timeout -> prior Hue state restored only if effects still own the lights.
17. User adjusts Hue while session active -> manual changes survive session cleanup.
18. Effects speaker fails -> doorbell greeting works; no unbounded retry.
19. Sound effect ends -> audio gate/echo guard prevents its transcription.
20. Quiet hours / effects disabled -> no loud stinger; light safety baseline maintained.

## 13. Operational metrics

Track: webhook acceptance/rejection/queue depth, effect dispatch and restore success, HA latency, speaker playback/interruptions; event ingestion/deduplication counts; time to first greeting; AI stage latency; playback duration/failures; microphone reconnects; VAD no-speech ratio; STT errors; sessions completed/aborted; fallback counts. Use structured logs with ephemeral session ID, never visitor identity. Default not to store media or transcripts.

## 14. Privacy and child-safety considerations

- Visitors may be minors. Use family-friendly prompts and fail closed on questionable responses.
- Clearly disclose AI audio interaction on the porch; review applicable audio capture/recording and consent laws.
- Do not collect identifying data or use facial recognition.
- Only activate STT during an active visitor session; avoid always-on cloud audio transmission.
- Keep images/audio/transcripts ephemeral; provide explicit opt-in and retention expiry if debugging is later enabled.

## 15. Open questions / assumptions to verify

- What UniFi Protect controller version/firmware is installed?
- Is the camera's audio stream available via the supported path, and what codec/sample rates?
- Does `uiprotect` talkback reliably support blocking playback on this exact setup?
- Is the trigger based on person presence, arrival zone, or doorbell press? Default: person detection in porch zone.
- Are outdoor audio volume and echo acceptable using the existing doorbell speaker?
- Should the application engage only on Halloween evening or by a schedule? Default: manually enabled feature flag.
- Is the intended default provider OpenAI for all AI stages? Yes, but leave adapters swappable.
- Which HA lights/groups and Hue scene should change? Configure entity IDs; do not guess.
- Which HA media player, if any, should play stingers, and where are licensed effect files hosted?
- Should Hue restore exactly previous state or a designated Halloween idle scene? Default: conditional previous-state restore.
- What quiet-hour schedule and maximum porch volume are appropriate?

## 16. Initial Codex instruction

> You are implementing the project described in `SPEC.md`. Start with **Phase A only**. Use **UniFi Protect Alarm Manager HTTP POST webhooks** as the primary trigger and **existing Home Assistant for Hue scene/lighting and optional sound effects**, not a custom Hue SDK. Scaffold the Python 3.12/FastAPI/Docker Compose project, the UniFi Protect adapter boundary, and diagnostic scripts for webhook receipt/validation, snapshots, talkback playback, authenticated incoming microphone stream verification, and Home Assistant arrival-scene/conditional-restore testing. Include unit tests with fakes and clear setup instructions. **Do not claim camera/audio functionality has been verified without actual hardware test results.** Do not implement the full AI conversation engine until the hardware integration spike is validated. Do not hard-code secrets, undocumented API paths, or unsupported model IDs. Produce a compatibility report template documenting Protect version, camera firmware, tested `uiprotect` version, and audio findings. Run tests and report remaining manual hardware steps.

## 17. Definition of done

A Dockerized, reproducible service runs autonomously for a Halloween session: receives and validates a UniFi person webhook, immediately triggers Hue arrival lighting via Home Assistant, optionally plays a short effect on a dedicated HA speaker, detects an arrival, classifies costume conservatively, speaks via G4 Doorbell Pro, listens to at most three visitor replies via verified mic stream, answers in character without talking to itself, enforces timeouts/departure/cooldown, restores controlled lights safely after completion, and fails gracefully without keeping visitor media by default.

## 18. Home Assistant reference interaction examples

### Script invocation from Docker (illustrative)

```http
POST /api/services/script/turn_on HTTP/1.1
Host: homeassistant.local:8123
Authorization: Bearer <HA_LONG_LIVED_ACCESS_TOKEN>
Content-Type: application/json

{
  "entity_id": "script.halloween_arrival_effect",
  "variables": {"session_id": "opaque-session-id"}
}
```

`script.halloween_arrival_effect` should own the actual Hue scene activation, optional stinger, and per-device safety rules. A corresponding `script.halloween_end_effect` handles conditional restore. For a simpler v1, use `light.turn_on` for a single dedicated porch light and save its prior `state` + color/brightness attributes in the app; exclude manually modified lights from automatic restore. Test state tracking on the installed Hue integration because some attributes may not round-trip exactly.

For speaker playback with `media_player.play_media`, `media_content_id` must be reachable by the playback system, often HTTP(S) content; verify integration-specific formats. Do not attempt to send a Docker-local path directly to a network speaker.

### Minimum acceptance demonstration

1. Start with Hue porch light warm white at 60%.
2. A real person event arrives via a Protect POST webhook; request is authenticated, scoped and queued.
3. Within a practical measured target (e.g. ~1 s of webhook acceptance), Hue transitions to orange/purple; an optional short stinger plays and finishes.
4. The Docker AI proceeds to snapshot and spoken costume greeting; STT only listens after stinger/talkback echo guard.
5. Conversation completes or times out; prior light state returns *unless another actor adjusted it*.
6. Duplicate Protect events never retrigger stingers or restart the session.
7. With Home Assistant disconnected, the greeting and dialogue continue (with effects degraded).

## 19. References and implementation verification

- UniFi Protect custom webhook setup: https://help.ui.com/hc/en-us/articles/25478744592023-Send-UniFi-Protect-Alerts-to-Web-Services-using-Webhooks
- UniFi Alarm Manager overview: https://help.ui.com/hc/en-us/articles/27721287753239-UniFi-Alarm-Manager-Customize-Alerts-Integrations-and-Automations-Across-UniFi
- Home Assistant Hue integration and scene support: https://www.home-assistant.io/integrations/hue/
- HA scenes / temporary state snapshots: https://www.home-assistant.io/docs/scene/
- HA REST API: https://developers.home-assistant.io/docs/api/rest/

All example entity IDs, URLs, scene names and webhook configuration fields are illustrative; validate against the deployed Protect and Home Assistant versions.
