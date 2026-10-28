# Halloween AI Doorbell — Engineering Handoff

**Status:** Design / unimplemented  
**Target hardware:** Ubiquiti UniFi G4 Doorbell Pro with UniFi Protect  
**Deployment:** Self-hosted Docker Compose; Python 3.12  
**Mode:** Interactive, half-duplex; up to three visitor replies  
**Date:** 2026-10-28

## 1. Executive summary

Build a self-hosted Halloween porch experience. On a qualifying UniFi Protect person-detection event, capture a camera image, identify or describe a possible Halloween costume with a vision-capable model, generate a short family-friendly character greeting, and play it through the G4 Doorbell Pro speaker. Then listen for the visitor's response via the doorbell microphone, transcribe it, and reply in character. Support at most three visitor replies per session, with strict timeouts and a greeting-only fallback.

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
- Run as a maintainable, testable Docker Compose workload without Home Assistant.

### Non-goals (v1)
- Identifying visitors by face, name, voiceprint, or biometric recognition.
- Always-on cloud streaming or continuous recording/retention.
- Full-duplex or interruption/barge-in handling.
- Storing visitor photos, audio, or transcripts long-term.
- Public web dashboard, multi-camera distribution, or Kubernetes.
- Automatic control of an animatronic or external speaker.

## 3. Reference architecture

```text
UniFi Protect G4 Doorbell Pro
  | person/doorbell events -> ProtectEventAdapter
  | image snapshots ------> CameraSnapshotAdapter ----> VisionAdapter
  | microphone audio -----> Authenticated stream -> FFmpeg -> PCM -> VAD -> STT
  ^ speaker talkback <----- AudioOutputAdapter <----- SpeechSynthesisAdapter

                      SessionController (single active session)
                         | State machine + timers
                         | ConversationAdapter (LLM)
                         | Ephemeral costume + dialogue history
                         | Metrics / structured logs

                       FastAPI local-only control API
```

### Primary boundaries
- `ProtectAdapter`: authenticates, receives events, captures snapshots, retrieves stream URL and provides talkback operations. Must hide differences between public Protect API and any library-specific implementation.
- `AudioInputAdapter`: owns FFmpeg lifecycle, bounded audio buffers, PCM conversion, VAD, microphone health.
- `AudioOutputAdapter`: accepts a generated audio file and plays it via talkback; reports actual completion or failure.
- `VisionAdapter`: returns structured costume description with explicit uncertainty, never identifies real people.
- `ConversationAdapter`: generates short, safe responses using costume context and recent session dialogue.
- `SpeechSynthesisAdapter`: produces audio supported by talkback, with format conversion if needed.
- `SessionController`: sole owner of transitions, listening gates, deadlines, concurrency, retries and cleanup.

**Provider models:** Choose currently supported model identifiers from up-to-date official documentation at implementation time. Do not copy illustrative model IDs from earlier brainstorming. Pin SDK and provider versions once validated.

## 4. UniFi hardware/API validation — mandatory first milestone

UniFi Protect firmware/API behavior can differ by controller and camera version. Before implementing conversational AI, produce a reproducible hardware-compatibility report containing:

1. Protect version, camera firmware, G4 Doorbell Pro model/variant, relevant network topology (without publishing secrets).
2. A successful authenticated camera event subscription or documented webhook and a real person event payload (redacted).
3. A successfully captured JPEG from the same camera.
4. A successful short WAV or compatible audio playback on the G4 Doorbell Pro speaker; report exact API/library version and audio format.
5. A successful **incoming microphone recording** from an authenticated stream. Verify audio track present using `ffprobe`, decode it with FFmpeg and transcribe a controlled test phrase.
6. An explicit report if the audio input and talkback paths interfere or audio feedback occurs.

Do not conflate the **outgoing talkback session** with the **incoming microphone stream**. Test them separately. If the mic cannot be consumed reliably, retain greeting-only mode and implement the mic as a pluggable adapter for later replacement.

Do not disable TLS verification by default. Use official Protect documentation and inspect installed `uiprotect` signatures before assuming exact method names or `play_audio()` arguments.

## 5. Visitor interaction and behavior

- Person enters a configured porch ROI; Protect reports person detection.
- Debounce nearby events and capture up to three frames over a short window; use best available frame.
- Determine costume by description, recognizable character where supported, or `unknown`. Never claim certainty based on a poor image.
- Generate a friendly character greeting, ideally 10–20 words, under 25 words maximum.
- Close input processing gate, clear buffers, synthesize/play greeting, await confirmed playback completion, discard a ~400 ms echo tail.
- Open input gate and listen for visitor speech (8 s timeout; VAD detects a completed utterance). Transcribe and respond.
- Repeat for up to three visitor replies. End sooner on no answer, a goodbye, departure, failure, or hard session deadline.
- After final response, gracefully close and suppress redetection until cooldown and occupancy rules permit a new session.

Character example: playful haunted-house butler. Must be age-appropriate. Do not ask visitors for names, ages, addresses, contact details, or other personal information. No insults or comments about bodies. Treat speech from visitors as untrusted input: it cannot override system rules or tool permissions.

## 6. State machine

States:
- `IDLE`: camera monitoring, no active conversation.
- `OBSERVING`: capture snapshots; classify costume with timeout.
- `GENERATING_GREETING`: generate opening text and audio.
- `SPEAKING`: input gate closed; output playback in progress.
- `LISTENING`: output complete, audio tail discarded, input gate open.
- `PROCESSING_REPLY`: VAD utterance acquired, transcribe/generate/synthesize.
- `CLOSING`: cancel pending tasks, release resources, clear ephemeral buffers.
- `COOLDOWN`: suppress events until cooldown expires and porch clear.

Transitions:
```text
IDLE --person/press--> OBSERVING --> GENERATING_GREETING --> SPEAKING
SPEAKING --confirmed completion and echo guard--> LISTENING
LISTENING --speech complete--> PROCESSING_REPLY --> SPEAKING
LISTENING --silence timeout--> CLOSING
PROCESSING_REPLY --turn limit reached--> SPEAKING(final) --> CLOSING
Any active state --error, deadline or cancellation--> CLOSING
CLOSING --> COOLDOWN --> IDLE (only once safe to retrigger)
```

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
    protect/{client,events,camera}.py
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
    test_audio_gate.py
    test_conversation_contracts.py
    test_failures.py
```

Dependencies: Python 3.12, FastAPI/Uvicorn, compatible pinned `uiprotect`, OpenAI SDK, Pydantic Settings, HTTPX, FFmpeg, VAD library, pytest/pytest-asyncio. Investigate exact current provider API calls before coding. Keep the container non-root, localhost-bind management endpoint, and restrict writable paths to tmpfs/cache. Protect test/config endpoints with authentication and do not expose them on the public internet.

## 11. Build sequence and acceptance gates

### Phase A — Hardware integration spike (first PR)
- Minimal Docker Compose, `ffmpeg`/`ffprobe` and basic connectivity diagnostics.
- Connect to Protect; discover camera ID; dump redacted capability report.
- Capture snapshot, subscribe to a person event.
- Play a short test phrase from an approved audio file.
- Capture 5–10 seconds of microphone audio and transcribe a known test phrase.
- Document what works, exact library/firmware versions and blockers.

**Gate:** Both talkback and microphone are confirmed in an actual test before turning on interactive mode. Otherwise proceed with greeting-only mode while preserving audio interfaces.

### Phase B — Costume greeting MVP
- Add vision classification with structured outputs and uncertainty.
- Generate a bounded, family-friendly greeting.
- Synthesize speech and play it through verified output adapter.
- Add duplicate-event suppression, cooldown, safe fallbacks, logs.

**Gate:** A real arrival causes exactly one appropriate greeting with no manual step.

### Phase C — Interactive conversation
- Continuous FFmpeg audio reader, bounded PCM buffers, VAD, STT.
- State machine, half-duplex gate, echo suppression, deadlines.
- Conversation history; max three visitor replies; goodbye-on-last-turn.
- Cancel/cleanup under exceptions, departure and timeouts.

**Gate:** Three-turn controlled exchange succeeds without the system transcribing its own audio or repeating greetings.

### Phase D — Reliability and privacy
- Repeat with darkness, groups, quiet speakers, background noise and no reply.
- Test Protect loss, network loss, STT/LLM/TTS errors, stream restarts.
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

## 13. Operational metrics

Track: event ingestion/deduplication counts; time to first greeting; AI stage latency; playback duration/failures; microphone reconnects; VAD no-speech ratio; STT errors; sessions completed/aborted; fallback counts. Use structured logs with ephemeral session ID, never visitor identity. Default not to store media or transcripts.

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

## 16. Initial Codex instruction

> You are implementing the project described in `SPEC.md`. Start with **Phase A only**. Scaffold the Python 3.12/FastAPI/Docker Compose project, the UniFi Protect adapter boundary, and diagnostic scripts for event subscription, snapshots, talkback playback, and authenticated incoming microphone stream verification. Include unit tests with fakes and clear setup instructions. **Do not claim camera/audio functionality has been verified without actual hardware test results.** Do not implement the full AI conversation engine until the hardware integration spike is validated. Do not hard-code secrets, undocumented API paths, or unsupported model IDs. Produce a compatibility report template documenting Protect version, camera firmware, tested `uiprotect` version, and audio findings. Run tests and report remaining manual hardware steps.

## 17. Definition of done

A Dockerized, reproducible service runs autonomously for a Halloween session: detects an arrival, classifies costume conservatively, speaks via G4 Doorbell Pro, listens to at most three visitor replies via verified mic stream, answers in character without talking to itself, enforces timeouts/departure/cooldown, and fails gracefully without keeping visitor media by default.
