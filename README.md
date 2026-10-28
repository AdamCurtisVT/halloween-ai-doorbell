# Halloween AI Doorbell

Python 3.12/FastAPI service for a UniFi G4 Doorbell Pro: person detection → costume description → greeting → optional half-duplex conversation with at most three visitor replies.

**Development proceeded on assumed hardware compatibility at the user's request. No real controller, camera, talkback, microphone, or live AI provider has been verified.** The compatibility report remains unvalidated. Automatic operation is off by default; tests and simulations use fakes.

## What is implemented

- Replaceable Protect, provider, incoming audio and outgoing audio boundaries; no hard-coded Protect endpoint paths.
- Authenticated Alarm Manager webhooks as primary ingress, bounded queue, scoped timestamp validation, duplicate suppression, one active session, cooldown and explicit inactivity admission; optional WebSocket backup.
- Home Assistant arrival lighting concurrent with observation, conditional per-light restore, optional dedicated-speaker stingers with quiet hours and a pre-speech stop barrier.
- OpenAI structured costume descriptions and short dialogue; output validation/moderation, uncertainty, generic fallbacks and no tools available to visitor speech.
- Continuous FFmpeg microphone consumption, 16 kHz mono PCM, WebRTC VAD, bounded buffering, EOF/stall recovery, closed input gate during generation/playback and an echo guard before listening.
- Three visitor replies maximum, 45-second default deadline, silence/unintelligible/degraded-input handling, explicit cancellation and a cached spoken goodbye before normal final teardown.
- Non-root/read-only Docker service, file-mounted secrets, authenticated operational controls, allowlisted state logs and in-memory counters/timing totals.
- Phase A diagnostics, an optional offline transcription helper and a deterministic simulation requiring no hardware or API key.

Physical departure, correct ROI behavior, volume, echo, microphone timing and real provider latency still require field validation. There is no full-duplex audio, face recognition, dashboard, long-term visitor storage or automatic scheduling.

## Install and test without hardware

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
python -m pytest -q
python -m scripts.simulate
cp .env.example .env
chmod 600 .env
```

Keep `FEATURE_ENABLED=false` while preparing. No Protect/provider connection or microphone reader starts in that mode. `scripts.simulate` uses fake camera/provider/audio adapters; it checks three listening turns, final playback and cleanup without touching real hardware. Do not use simulation results as compatibility evidence.

GitHub Actions runs tests, the simulation, Compose validation, a Docker build and a restricted-container smoke test on push/PR. It needs no secrets and publishes no image. Dependencies are pinned in `requirements.lock`; `uiprotect==25.0.0` targets current Protect releases (upstream lists 7.2+). Older installations may need adapter/library changes validated separately.

## Configure and deploy

See [runtime configuration and operations](docs/runtime.md) for activation, model choices, controls, fallbacks, privacy and known limitations.

Configure the Protect console hostname (without scheme), local user/password, camera ID, API key and a random control token privately. Use the narrowest working permissions. Cloud login/MFA is not supported. Prefer trusted console certificates; do not disable TLS verification for deployment. Install the console CA in the host/container trust store as needed.

```sh
# Disabled service: health/status only, no camera/provider activity.
docker compose up --build -d
curl http://127.0.0.1:8000/health
```

Compose binds the management port to loopback, drops capabilities, uses a read-only root and provides bounded /tmp tmpfs. Do not expose the management port publicly. `docker compose down` stops the service. Do not publish resolved `docker compose config` output; it can include environment secrets.

### Secret files

The credential settings `UNIFI_USERNAME`, `UNIFI_PASSWORD`, `UNIFI_API_KEY`, `UNIFI_AUDIO_STREAM_URL`, `CONTROL_API_TOKEN`, `OPENAI_API_KEY`, `UNIFI_WEBHOOK_TOKEN` and `HOME_ASSISTANT_TOKEN` each accept a corresponding `_FILE` setting. Files are read once at startup; restart after rotation. Nonempty direct values plus `_FILE` are rejected. Missing/empty/invalid/oversized files fail closed with masked errors. Trailing CR/LF is removed; spaces are preserved.

`docker-compose.secrets.yml` is a **standalone alternative**, not an override:

1. Create `secrets/` with directory mode `0700`.
2. Privately create `unifi_username`, `unifi_password`, `unifi_api_key`, `control_api_token`, `openai_api_key` and `unifi_webhook_token` files containing only the values.
3. Ensure mounted files are readable by container UID 10001. For local Compose file mounts, mode `0444` inside the host's `0700` directory permits the container to read them without allowing other host users to traverse that directory. These are file mounts, not an encrypted secret store.
4. Keep nonsecret settings in `.env`, then run:

   ```sh
   docker compose -f docker-compose.secrets.yml up --build -d
   ```

This variant passes only explicit nonsecret settings and file paths, rather than injecting `.env`. The base secret template requires Protect credentials/API key, the control/OpenAI tokens and the webhook token. It no longer requires microphone or HA secret files when those features are unused.

Add `docker-compose.ha-secret.yml` for HA effects and `docker-compose.microphone-secret.yml` for interactive microphone input. Create `secrets/home_assistant_token` and `secrets/unifi_audio_stream_url` only for the overrides you enable. For example, the full interactive/effects deployment is:

```sh
docker compose -f docker-compose.secrets.yml \
  -f docker-compose.ha-secret.yml -f docker-compose.microphone-secret.yml \
  -f docker-compose.runtime.yml up --build -d
```

Both variants now preserve `EFFECTS_QUIET_HOURS=` as an explicit request to disable stinger quiet hours. TLS verification stays enabled in the secrets variant. All referenced secret files must be nonempty. If using WebSocket-only or disabled diagnostic-only operation, remove unused mandatory secret references rather than creating dummy credentials. `secrets/`, `models/`, `.env` and generated media are excluded from Git/build context.

## Phase A hardware diagnostics

Run from the repository with the core virtualenv active. FFmpeg/ffprobe must be on PATH or use the Docker image. CLI errors intentionally omit raw third-party exceptions that might contain credentials. Exit code 1 means incomplete/failure; 130 means interrupted.

Before starting real triggers, configure the [webhook and effects integration](docs/webhooks-effects.md). The revised primary path is Alarm Manager POST; WebSocket event subscription below is an optional independent diagnostic.

1. **Discover:** `python -m scripts.check_protect --list-ids`. This reveals camera IDs locally: do not share the output. Set `UNIFI_CAMERA_ID`. The default report contains hashed camera references, versions, model, speaker flag, mic volume and channel dimensions; capability flags are not proof of working audio.
2. **Events:** `python -m scripts.check_protect --events-seconds 60`. Trigger a real person detection on the selected camera. Preserve the sanitized event projection. Zero events fails the diagnostic. Configure porch detection zones in Protect; this application consumes those events and does not invent camera ROI settings.
3. **Snapshot:** create a private `artifacts/` directory, then run `python -m scripts.check_protect --snapshot artifacts/snapshot.jpg`. Visually inspect the same-camera JPEG, then delete it. Existing files are not overwritten.
4. **Talkback:** review a short approved PCM WAV, then run `python -m scripts.test_talkback /absolute/path/test.wav --approve-playback`. An observer must confirm audibility, format, timing, volume and distortion. Library completion is not proof of audible output.
5. **Microphone:** obtain a supported authenticated RTSP(S) URL from your installation and put it privately in `UNIFI_AUDIO_STREAM_URL`. No path is guessed or obtained from talkback. Run the controlled phrase diagnostic below.
6. **Authentication:** in a private controlled test confirm the stream rejects missing/invalid credentials. A URL with userinfo/query token is only a configuration check; successful decoding alone does not prove enforced authentication.
7. **Interference:** capture microphone audio during/after talkback in a second terminal. Use different speaker and visitor phrases; record continuity, echo, feedback and latency. Repeat to assess consistency and cleanup.

Copy [the compatibility template](docs/compatibility-report-template.md) into a private report. Record actual Protect/camera firmware/library versions, evidence and blockers. Both audible output and authenticated incoming speech need actual hardware evidence before claiming the interactive hardware gate passed.

RTSP exposes credentials/media on the local transport; prefer RTSPS with certificate verification (explicitly enabled). URL credentials may be visible to local process inspection, so use a trusted host. `UNIFI_VERIFY_SSL=false` is only an explicit library troubleshooting option; it does not disable microphone TLS verification.

Docker diagnostics: `docker compose run --rm spike python -m scripts.check_protect ...`. Mount an approved WAV read-only with `-v /absolute/test.wav:/media/test.wav:ro` before `spike` for talkback. /tmp artifacts disappear when the container exits; use an explicit private mount if retaining a snapshot for inspection.

### Prepare fallback/test speech

On macOS, `python -m scripts.prepare_audio` generates three local WAVs without playing them: `approved-test.wav`, `fallback.wav`, `goodbye.wav` under `artifacts/`. Existing files are kept. On other hosts record or synthesize the phrases and convert to short PCM WAVs with FFmpeg. Review the content before enabling playback.

Generated here: speaker test 3.293 s, fallback 4.605 s, goodbye 4.019 s; all mono 16 kHz, 16-bit PCM. Only file format/duration was checked; no acoustic verification occurred. The generated files are gitignored and must be prepared on each deployment host.

### Optional local transcription diagnostic

The microphone diagnostic accepts any trusted executable taking one WAV path and returning only transcript text on stdout. The bundled `scripts/transcribe-wav` uses local Faster Whisper CPU/int8 inference, requires an existing converted model directory and disables model downloads. It is separate from the live runtime's OpenAI transcription adapter.

```sh
python3.12 -m venv .venv-stt
.venv-stt/bin/pip install -r requirements-transcription.txt
export STT_MODEL_DIR=/absolute/path/to/converted-model
PATH="$PWD/.venv-stt/bin:$PATH" scripts/transcribe-wav artifacts/approved-test.wav
PATH="$PWD/.venv-stt/bin:$PATH" .venv/bin/python -m scripts.test_microphone \
  --approve-recording --seconds 7 \
  --expected-phrase 'Happy Halloween from the porch' \
  --transcriber "$PWD/scripts/transcribe-wav"
```

Obtain compatible converted weights separately and record their source/version. The helper follows the [Faster Whisper documentation](https://github.com/SYSTRAN/faster-whisper). Optional dependencies/weights are not installed or recognition-validated here and are not included in the base Docker image. The standalone helper prints transcripts; use synthetic speech for preparation. In the microphone diagnostic, transcripts are compared privately and not printed/stored. Model loading plus inference must finish within its 60-second timeout.

The diagnostic probes an incoming audio track, decodes 5–10 seconds to 16 kHz mono PCM, rejects early EOF, transcribes a temporary WAV and compares normalized words. Its temporary directory is removed on normal/error/cancellation paths; forced termination/power loss may leave files on local disk, so prefer tmpfs. External transcribers must honor your retention policy.

## Validation status

Verification: **125 tests passed locally; 121 passed in the restricted Linux container, with four host-only Compose checks skipped**; the full-session simulation passed in both environments. The container ran non-root with a read-only root, dropped capabilities and bounded tmpfs. Both Compose variants (including media mounts) validated. Tests cover provider payloads/refusals/moderation, three-turn limits and near-deadline cancellation, final goodbye, echo guard, duplicate/inactivity handling, cancellation, secret files, audio overflow/EOF, process cleanup and authenticated controls. No live provider calls or hardware diagnostics were performed. Docker/CI results are software checks, not hardware evidence.

Provider integration references: [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), [vision](https://developers.openai.com/api/docs/guides/images-vision), [speech synthesis](https://developers.openai.com/api/docs/guides/text-to-speech), [transcription](https://developers.openai.com/api/docs/guides/speech-to-text). Protect references: [uiprotect](https://github.com/uilibs/uiprotect), [Ubiquiti public API](https://developer.ui.com/protect/). Library signatures were inspected locally; firmware compatibility remains unverified.


## Configure once, then test the complete app

The [acceptance runbook](docs/acceptance.md) is the sequential path from offline checks through a complete real session and fault testing. Run `python -m scripts.preflight` for an offline report of all missing settings/media/tools. Add `--live-protect` or `--provider` deliberately for read-only remote checks; neither records, plays audio or generates provider content. Model catalog access is not a capability test.

`python -m scripts.control status` reads the configured control token without exposing it as a CLI argument. Commands include `preflight`, `ready`, `enable`, `test-session`, `cancel`, `disable`. Enable/manual-test require `--approve-camera-audio`. Their use can start recording, media uploads and speaker playback. The CLI accepts numeric loopback IP URLs only and does not follow redirects or use environment proxies.

The app supports runtime re-enable, manual full-session tests, subscription-aware readiness, three predefined personas, configurable VAD and bounded snapshot selection. Software implementation is ready for integrated testing against the configured hardware/provider stack; physical acceptance and any resulting fixes remain open.

Compose permits up to three process-failure restarts and gives shutdown 60 seconds for resource cleanup. The image has a liveness healthcheck; readiness remains the authenticated API check. CI also starts a real isolated HTTP server in the restricted container to verify startup, authentication, disabled controls and shutdown without real credentials/media.

Final operational verification also passed: three-reply fake simulation and isolated real HTTP startup/authentication/preflight/control/shutdown smoke test in Docker. No real camera or provider calls were made.


## Revised webhook + Home Assistant architecture

Start the complete app with `python -m app.server` or Compose. One process shares one runtime across control port 8000 and webhook port 8080. Controls remain mapped to `127.0.0.1:8000`; only the webhook listener maps to `${DOCKER_LAN_IP}:8080`. Set that variable to a specific LAN interface and firewall it to Protect (or your explicitly trusted LAN proxy). The unconfigured default stays loopback. Do not expose either port to the internet; do not start a second conversation automation in Home Assistant.

Read [webhook/effects setup and restoration limits](docs/webhooks-effects.md). New diagnostics: `python -m scripts.check_webhook --bind <LAN-IP> --seconds 60` (standalone authenticated receiver, no AI/lights/audio) and `python -m scripts.check_effects` (HA read-only), adding `--approve-effects` only for controlled lighting/stinger/restore tests. Real hardware/HA acceptance is still outstanding.


Final test-readiness hardening: WAV assets are checked for complete audio data, mono/stereo PCM, 8-192 kHz, <=10 seconds and <=2 MB. Shutdown cannot race with a new enable request, streamed webhooks recheck runtime availability before enqueue, and concurrent stinger-stop barriers are serialized. No hardware/provider acceptance is implied by these checks.

The four Compose checks passed on the host and run in the CI host job; the app image intentionally contains no Docker CLI. Final testing image: `halloween-doorbell:testing`. Dependency checks, the dual-listener HTTP smoke test, and the three-reply simulation also passed. No hardware/HA/provider acceptance test was run.
