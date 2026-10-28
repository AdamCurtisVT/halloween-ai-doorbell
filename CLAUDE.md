# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Python 3.12 / FastAPI service for a UniFi G4 Doorbell Pro: a person-detection webhook triggers a costume description, a spoken greeting and an optional half-duplex conversation of up to three visitor replies. Home Assistant lighting and sound effects run alongside it. `SPEC.md` is the design handoff and the source of truth for intended behavior.

**No real hardware, Protect controller, Home Assistant instance or live AI provider has been verified.** All tests and the simulation use fakes. Do not present simulation or CI results as hardware compatibility evidence. `/status` and the control endpoints deliberately report `hardware_verified: False`.

## Commands

```sh
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock           # pinned deps (incl. pytest); pyproject has the same pins
python -m pytest -q                        # full suite (asyncio_mode=auto)
python -m pytest tests/test_sessions.py::test_echo_guard_precedes_open_gate -q   # single test
python -m scripts.simulate                 # deterministic fake full session (3 replies), no network
python -m scripts.smoke_server --dual-listener   # real HTTP startup/auth/shutdown smoke test
python -m scripts.preflight                # offline config/media/tool report; --live-protect / --provider for read-only remote checks
python -m app.server                       # full app: control :8000 + webhook :8080, one shared Runtime
docker compose config --no-env-resolution --quiet   # Compose validation (CI also checks docker-compose.secrets.yml)
```

CI (`.github/workflows/ci.yml`) runs pytest, the simulation, Compose validation, a Docker build and smoke tests in a restricted container (read-only root, `--cap-drop ALL`, uid 10001). Compose tests in `tests/test_operations.py` are skipped when the `docker` CLI is absent. Keep code working under a read-only root filesystem with only `/tmp` as tmpfs.

Other diagnostics in `scripts/` (`check_protect`, `check_webhook`, `check_effects`, `test_microphone`, `test_talkback`, `control`) touch real devices. Flags such as `--approve-effects` and `--approve-camera-audio` exist to gate side effects. Don't run them unless asked.

## Architecture

**Process layout** (`app/server.py`): one process runs two uvicorn listeners that share one `Runtime`:
- `app/main.py` `create_app()` is the bearer-authenticated control API on port 8000 (`/status`, `/ready`, `/preflight`, `/enable`, `/test/session`, `/cancel`, `/disable`). `/enable` and `/test/session` require the body `{"approve_camera_audio": true}`. Compose binds this listener to loopback only.
- `app/protect/webhook.py` `create_webhook_app()` is the Alarm Manager webhook receiver on port 8080 (LAN-exposed). It checks token, source IP, camera, alarm name, trigger, body size and timestamp skew, deduplicates by time bucket, normalizes the payload into `PersonEvent` and enqueues it on a bounded `WebhookInbox` queue.
- `app.main:app` is a module-level instance used by tests and the container healthcheck. It is not the production entrypoint.

**Runtime** (`app/runtime.py`) owns enable/disable under a lock and a supervise loop with exponential backoff (1 s up to 30 s). Each connection cycle builds a fresh Protect client, `OpenAIProvider`, `LiveMicrophone` (interactive mode only), effects adapter, `SessionController` and `SessionManager`, then feeds events from the webhook inbox, the Protect WebSocket or both (`TRIGGER_SOURCE`). `validate()` enforces the required config and allowlists before activation. Nothing connects while `FEATURE_ENABLED=false`, which is the default.

**Admission** (`app/sessions/manager.py`) allows one session at a time. It ignores duplicate `event_ref`s, applies a cooldown and requires an "inactivity" re-arm: the porch must be quiet for `porch_clear_seconds`. Detection-end events are intentionally ignored because they don't prove the visitor left. After a reconnect the manager starts disarmed.

**Session state machine** (`app/sessions/controller.py`, `State` enum): APPROACH_EFFECT → OBSERVING (snapshots) → GENERATING_GREETING (one `provider.greet(jpeg)` call returns costume + greeting; an optional `ARRIVAL_WAV` plays concurrently) → SPEAKING → LISTENING/PROCESSING_REPLY loop (max `max_visitor_replies`, one `_turn()` each) → CLOSING → COOLDOWN. Key invariants that tests depend on:
- Half-duplex: the microphone gate is closed during generation and playback, and the echo-guard sleep runs *before* the gate opens.
- Every provider call is wrapped in `provider_timeout_seconds`, and the whole session in `max_session_seconds`. Each stage falls back instead of failing silently: greet failure → plain `reply()` greeting for `Costume.unknown()` → static text with `FALLBACK_WAV`.
- Each turn runs under a budget of remaining time minus `goodbye_reserve_seconds`, and starts only if at least `min_turn_seconds` is available; running out plays `GOODBYE_WAV` instead of hitting the hard deadline.
- A final or ending reply is always followed by the cached goodbye WAV. A fourth listen must never open.
- `LiveMicrophone.listen(seconds)` bounds the wait for speech *onset*; the utterance itself runs to trailing silence or 8 s.
- Per-session data (history, temp WAVs) lives in a `TemporaryDirectory` and is cleared in `finally`.

**Pluggable boundaries.** Each one is a `Protocol` with a real implementation, and tests substitute fakes:
- `app/ai/contracts.py`: `Provider` protocol plus the `Costume`/`Reply`/`Greeting` pydantic models. `Reply` validation (word/length/question limits, `UNSAFE` regex) is defense in depth on every model output. `openai_provider.py` is the only OpenAI-aware module. TTS requests raw 24 kHz PCM and writes the WAV header locally.
- `app/protect/client.py`: the only module that imports `uiprotect`. Don't hard-code Protect endpoint paths anywhere.
- `app/audio/`: `live.py` runs FFmpeg to produce 16 kHz mono PCM with WebRTC VAD, a bounded queue and EOF/stall reconnects. `input.py` validates the stream URL. `output.py` validates WAV assets (PCM, 8–192 kHz, ≤10 s, ≤2 MB).
- `app/effects/`: `EffectsAdapter` protocol. `factory.make_effects` returns `NoOpEffectsAdapter` when effects are disabled or misconfigured, so the conversation continues without them. `EffectsCoordinator` wraps every call in a timeout (`effects_lifecycle_timeout_seconds` for arrival/end, `effects_timeout_seconds` otherwise) and counts failures as `effects_degraded` instead of raising. Approach lighting starts at session start, concurrently with observation. `before_speech` waits for it and stops any stinger sound before doorbell speech. `home_assistant.py` snapshots light state, records each light's *appearance* after the transition settles (Hue reports state after HA answers the call), and restores a light only if it still looks that way or its latest change carries one of the effect's own HA contexts. Its tests use `defer_state_updates()` to model that late reporting.

**Config** (`app/config.py`) is pydantic-settings loaded from env or `.env`. Secrets are `SecretStr`. Many also accept a `*_FILE` variant for Docker secrets mounted by the `docker-compose.*-secret.yml` overlays.

## Conventions

- Privacy: never log exception text, transcripts, images or raw identifiers. Hash identifiers with `reference()` and record outcomes as counters in `metrics` (`Counter`). Logging goes through the allowlist in `app/observability.py`. Third-party loggers are silenced when the runtime is enabled.
- Caught exceptions go through `observability.failure(stage, error)`, which logs and keeps (for `/status` `recent_failures`) only the stage, exception class and HTTP status.
- Visitor transcripts are untrusted data. They go into dialogue history only and are never treated as instructions. No tools are exposed to the dialogue model.
- New failure modes should degrade to a safe fallback, call `failure()` and increment a metric. Raise only for cancellation.

## Further docs

- `docs/runtime.md`: activation, models, controls, fallbacks, privacy, limitations
- `docs/webhooks-effects.md`: Alarm Manager / Home Assistant setup and restore limits
- `docs/acceptance.md`: sequential runbook from offline checks to real-hardware acceptance
