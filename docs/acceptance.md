# Full-app acceptance runbook

Build/configure first, then run this sequence against one deployment and record failures for fixes. No step below is already certified on real hardware. The application has no public dashboard or face recognition; its interface is the local API/CLI and physical doorbell. Keep the [compatibility report](compatibility-report-template.md) alongside private test notes.

## 1. Offline build and configuration

```sh
. .venv/bin/activate
pip install -r requirements.lock
python -m pytest -q
python -m scripts.simulate
# Optional real HTTP smoke, isolated from .env and hardware:
python -m scripts.smoke_server
python -m scripts.prepare_audio  # macOS only; otherwise supply reviewed short PCM WAVs
python -m scripts.preflight
```

Preflight exits 1 until all required configuration, short approved WAVs and FFmpeg tools exist. It prints safe check names/results, not secret values or paths. The default makes no remote calls and does not require FEATURE_ENABLED=true. For a Docker deployment, run preflight inside the container with the media mounts so paths/trust/tool checks match the runtime:

```sh
docker compose -f docker-compose.yml -f docker-compose.runtime.yml build
docker compose -f docker-compose.yml -f docker-compose.runtime.yml run --rm spike python -m scripts.preflight
```

Use `.env.example` and `docs/runtime.md` to fill missing settings. `FALLBACK_WAV` and `GOODBYE_WAV` must use container paths when testing in Docker. Select account-supported models from current official documentation; no model capability is inferred from its name.

## 2. Read-only remote checks

```sh
python -m scripts.preflight --live-protect --provider
```

This authenticates to Protect and checks the selected camera/speaker capability flag. It checks catalog access for each configured model via the provider's model retrieval API. It does not read snapshots, subscribe to events, record microphone audio, generate AI content or play audio. Successful model retrieval proves catalog access only, not input/output capability or generation quota. Run this inside Docker too if the deployment has different certificates/networking. Never share raw credentials, stream URLs or SDK debug output.

## 3. Establish actual compatibility evidence

Follow the README Phase A diagnostic sequence: person event, same-camera JPEG, audible approved WAV, incoming audio track, controlled phrase transcription, invalid-auth rejection, simultaneous input/output continuity and echo tests. Record Protect/firmware/uiprotect versions, observer findings and media deletion. A passing software test is not a substitute for this evidence.

The product can be built before these tests; describe failures as integration issues and fix the appropriate adapter. Do not mark hardware as verified based on assumptions.

## 4. Start the whole app in greeting-only mode

Configure `CONVERSATION_MODE=greeting_only`, retain `FEATURE_ENABLED=false` to start disarmed, and launch:

```sh
docker compose -f docker-compose.yml -f docker-compose.runtime.yml up --build -d
python -m scripts.control preflight
python -m scripts.control status
python -m scripts.control enable --approve-camera-audio
python -m scripts.control ready
python -m scripts.control test-session --approve-camera-audio
```

The CLI reads the control token from local settings; if using file secrets, make that file accessible to the CLI or run the CLI inside the service container. These commands use the default loopback port 8000. `enable` and `test-session` explicitly authorize real media/provider/speaker activity. Check status/logs for progression and the observed result. Readiness is software health only; `hardware_verified` remains false.

The manual test runs the normal controller and respects busy/inactivity/cooldown constraints. It should describe a possible costume cautiously and play one greeting. Test darkness, no costume and groups. Confirm no visitor media/transcripts remain in /tmp after teardown. Then wait for cooldown/quiet and cause a real person detection: exactly one greeting should play despite repeated updates. Verify the expected detection zones in Protect.

Use `python -m scripts.control cancel` during generation and playback: pending work should stop and enter cooldown. Use `python -m scripts.control disable` to stop camera/provider/microphone resources. Re-enable with the approval command. Disabling is not a persistent configuration change: restart follows FEATURE_ENABLED from configuration.

## 5. Enable interactive operation

Disable, configure `CONVERSATION_MODE=interactive`, `STT_MODEL` and authenticated stream URL, then recreate/restart the service to load the changed configuration. Enable with explicit approval. Validate microphone readiness before beginning a test session.

Test these independently:

- Three distinct visitor replies: each gets one contextual response; no fourth listening turn; final spoken goodbye before normal teardown.
- Silence and unintelligible speech: no invented reply, clean closure.
- Speech during speaker playback: discarded; never used as a visitor response.
- Speech after echo guard: captured/transcribed once, with no speaker leakage.
- Visitor goodbye: cached goodbye plays and session closes.
- Third response near the session deadline: deterministic cancellation, no hung playback or later listening.
- Visitor leaves: verify silence/deadline closure or explicitly cancel. Do not treat a detection-end update as physical departure.
- A new visitor after the inactivity interval and cooldown: one fresh session.

If input fails, subsequent sessions should deliver greetings while input is degraded, with counters indicating the failure. If talkback fails, the controller must never reopen listening for a broken conversation.

## 6. Fault and privacy tests

While using controlled test subjects, interrupt Protect networking, the incoming stream and provider access separately. Confirm bounded retries, degraded readiness, no overlapping sessions and recovery. Invalid output, moderation refusals and TTS failure should use safe fallbacks or close. Remove/restore credentials through configuration/restart, rather than publishing them in test logs.

Exercise duplicate storms, repeated session tests and cancellation during every stage. Watch `status` counters, process memory and /tmp usage across repeated runs. Logs/status must not contain photos, transcripts, credentials or media URLs. Temporary session files should disappear after normal/error/cancellation teardown; Docker tmpfs disappears on container removal. Provider retention is a separate policy review.

Complete the compatibility report and note any firmware-specific end-event behavior, required audio format, acceptable volume, VAD tuning and echo-tail settings. Finish visitor disclosure/consent and a real porch rehearsal before unattended operation. The results of this sequence determine the fix list; the software test suite does not pre-certify them.


## Revised trigger and effects acceptance

Before the greeting/interactive steps, complete [webhook and HA setup](webhooks-effects.md). With the app disabled, run the standalone authenticated webhook diagnostic on the LAN interface and trigger a real Alarm Manager Person Detection POST. Verify source hash, allowed camera/device mapping, exact alarm/trigger keys, timestamp validation and duplicate counts. Then stop the diagnostic so the app can bind 8080.

Run `scripts.check_effects` read-only, then `scripts.check_effects --approve-effects` with an observer. Start the porch light warm white at 60%; confirm the arrival scene/colors, then restoration. Repeat while manually adjusting the light during the effect: the manual change must survive. Repeat with HA unavailable and with a configured dedicated effects speaker. Confirm stinger volume/quiet hours, stop before doorbell speech/STT, and greeting-only degradation if its stop cannot be confirmed. Never play a Docker-local media path on a remote speaker.

With the full app enabled, verify arrival lights begin during observation, before inference finishes, and duplicate webhook/WS updates do not repeat lights/stingers. Test HA restart: ownership changes must skip stale restore and leave safe baseline lighting. Record latency, restoration/skips, source topology/proxy header support and all outstanding blockers in the compatibility report. These are actual acceptance tests, not claims made by the fake suite.

Before enabling hardware, verify your chosen Compose combination with `docker compose ... config --quiet`; do not share resolved environment output. The base secrets variant does not mount HA or microphone credentials: add the matching optional override(s). Test cancellation while a webhook body is still arriving, concurrent stinger-stop calls, and rejection of a deliberately truncated synthetic WAV. These cases have software regression coverage; record actual device behavior separately.
