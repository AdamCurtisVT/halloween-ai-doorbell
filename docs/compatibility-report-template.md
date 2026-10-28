# Phase A hardware compatibility report

Status: **NOT TESTED — template only**

Date/time/operator:
Git commit:
Test environment (OS/architecture, Python, Docker/Compose, FFmpeg/ffprobe/PyAV):
Installed `uiprotect` version (scaffold pins 25.0.0; actual tested version):
Protect console model / Protect version:
Doorbell exact model/variant / camera firmware:
Network topology / VLAN/firewall/routing (no IPs, credentials, serials or tokens):
TLS trust configuration / any explicit insecure experiment:
Local user permissions and API key requirements (no secrets):

| Test | Result (not tested/pass/fail) | Actual evidence and time | Blocker |
| --- | --- | --- | --- |
| Authenticated Protect login/discovery | not tested | | |
| Alarm Manager POST auth/source/scope/dedup + prompt 202 | not tested | | |
| Optional selected-camera WebSocket subscription | not tested | sanitized payload below | |
| Same-camera JPEG capture + visual review | not tested | | |
| Blocking talkback completion | not tested | | |
| Observer confirms audible approved phrase | not tested | | |
| Incoming audio track in ffprobe | not tested | sanitized codec/rate/channels only | |
| Invalid/removed stream auth is rejected | not tested | never paste URL | |
| 5–10 s incoming decode to PCM | not tested | decoded duration | |
| Controlled visitor phrase transcription | not tested | expected/actual match, private evidence | |
| Concurrent playback/input continuity | not tested | | |
| Echo/feedback, before/during/after playback | not tested | | |
| HA arrival cue + conditional restore | not tested | | |
| Manual Hue changes survive cleanup | not tested | | |
| HA unavailable/restarted safe degradation | not tested | | |
| Optional speaker / quiet hours / stop-before-STT | not tested | | |
| Repeated run and resource cleanup | not tested | | |

Sanitized person payload (never full bootstrap/session/URL):

```json
{}
```

Snapshot visual findings (same physical camera confirmed?); deletion time:
Talkback source WAV codec/rate/channels/duration, encoded format if observed:
Playback start/completion timing, measured audibility/volume/distortion:
Incoming stream provenance and authentication mechanism (no URL):
Audio track codec/rate/channels; decoded sample format and duration:
Transcriber executable/tool version, provider/model if used (verified supported), consent:
Controlled phrase result; physical microphone/source verification:
Input/output interference, dropouts, echo/feedback:
Failure behavior and reproducibility:
Retention/deletion confirmation (including external transcription tool):

Decision: NOT VALIDATED / greeting-only candidate / interactive hardware gate passed.
Evidence reviewer/date:
Remaining blockers and manual steps:

Do not mark the gate passed based on unit tests, capability flags, a process exit code,
or talkback completion alone. Both audible output and authenticated incoming speech
must have actual hardware evidence. Conversational software was later built on assumed compatibility; it does not validate this gate.


## Webhook and Home Assistant evidence

Protect POST payload shape and observed trigger key (redacted):
POST custom-header support on installed Protect version:
LAN bind, firewall/proxy trust boundary, hashed actual socket source:
Webhook accepted/rejected/duplicate/queue-full results and acknowledgement latency:
HA version / Hue integration version / designated porch entities (private):
Ownership style: direct HA REST (do not run competing arrival/end scripts on the same lights).
Scene scope verification if using imported Hue scene:
Arrival measured latency; sufficient stair/camera brightness:
Prior warm-white 60% -> arrival -> conditional restore findings:
Manual change during effect/transition survives restore:
HA restart/unreachable behavior and static safe-baseline recovery procedure:
Dedicated effects speaker, owned/licensed URL reachability, volume/quiet-hours tests:
Stinger completion/stop before speech, gate/echo findings, stop-failure greeting-only degradation:
Any REST compare/restore race or Hue attribute-round-trip limitations:
