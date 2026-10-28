import asyncio
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock
import pytest

from app.ai.contracts import Reply, Costume
from app.audio.live import LiveMicrophone, MicrophoneFailure, FRAME_BYTES
from app.config import Settings
from app.protect.client import PersonEvent, UiprotectAdapter
from app.sessions.controller import SessionController, State
from app.sessions.manager import SessionManager
from scripts.simulate import FakeMicrophone, fake_provider, fixture_wav


@pytest.fixture
def setup(tmp_path):
    fallback = tmp_path / 'fallback.wav'
    goodbye = tmp_path / 'goodbye.wav'
    fixture_wav(fallback)
    fixture_wav(goodbye)
    settings = Settings(_env_file=None, snapshot_interval_seconds=0, conversation_mode='interactive', audio_echo_guard_ms=0,
                        fallback_wav=str(fallback), goodbye_wav=str(goodbye),
                        cooldown_seconds=30, porch_clear_seconds=10)
    protect = Obj(snapshot=AsyncMock(return_value=b'jpeg'), play=AsyncMock())
    provider = fake_provider()
    mic = FakeMicrophone()
    return SessionController(settings, protect, provider, mic)


async def test_three_turns_final_goodbye_and_deletion(setup):
    c = setup
    paths = []
    async def play(path):
        assert not c.microphone.gate
        assert path.exists()
        paths.append(path)
    c.protect.play.side_effect = play
    await c.run()
    assert c.microphone.listen_count == 3
    assert c.provider.transcribe.await_count == 3
    assert c.provider.reply.call_args.args[-1] is True
    assert paths[-1] == Path(c.settings.goodbye_wav)
    assert not paths[0].exists()  # ephemeral speech file deleted
    assert not c.history and not c.microphone.gate
    assert c.state == State.COOLDOWN
    assert c.metrics['sessions_completed'] == 1


async def test_silence_never_transcribes(setup):
    c = setup
    c.microphone.utterances = [None]
    await c.run()
    c.provider.transcribe.assert_not_called()
    assert c.metrics['no_speech'] == 1


async def test_final_flag_does_not_discard_audio(setup):
    c = setup
    c.provider.reply.side_effect = [Reply(response_text='Welcome!', tone='friendly', ends_conversation=False),
                                   Reply(response_text='Have fun!', tone='friendly', ends_conversation=True)]
    await c.run()
    assert c.microphone.listen_count == 1
    assert c.protect.play.await_count == 3  # greeting, reply, guaranteed goodbye


@pytest.mark.parametrize('failure', ['vision', 'reply', 'synthesize', 'transcribe', 'play'])
async def test_stage_failures_cleanup_and_fallback(setup, failure):
    c = setup
    target = c.protect if failure == 'play' else c.provider
    getattr(target, failure).side_effect = RuntimeError('private detail')
    await c.run()
    assert not c.history and not c.microphone.gate
    if failure in {'reply', 'synthesize', 'play'}:
        assert c.microphone.listen_count == 0
    if failure == 'play':
        assert c.metrics['sessions_failed'] == 1
    if failure == 'vision':
        assert c.provider.reply.call_args_list[0].args[0].certainty == 'unknown'


async def test_mic_degraded_greeting_only(setup):
    c = setup
    c.microphone.healthy = False
    await c.run()
    assert c.protect.play.await_count == 1
    assert c.microphone.listen_count == 0
    assert c.metrics['microphone_degraded'] == 1


async def test_unintelligible_closes_without_reply(setup):
    c = setup
    c.provider.transcribe.return_value = ''
    await c.run()
    assert c.provider.reply.await_count == 1
    assert c.metrics['unintelligible'] == 1


async def test_deadline_and_cancel_clear_history(setup):
    c = setup
    started = asyncio.Event()
    async def stalled(*args):
        started.set()
        await asyncio.Event().wait()
    c.provider.reply.side_effect = stalled
    c.settings.max_session_seconds = 0.02
    await c.run()
    assert c.last_result == 'deadline'
    c.settings.max_session_seconds = 45
    started.clear()
    task = asyncio.create_task(c.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.last_result == 'cancelled'
    assert not c.history and not c.microphone.gate


async def test_concurrent_sessions_rejected(setup):
    c = setup
    async with c.lock:
        await c.run()
    assert c.metrics['sessions_busy'] == 1
    c.protect.snapshot.assert_not_called()


def event(ref, ended=False):
    return PersonEvent(ref, 'camera', 'smartDetectZone', ('person',), 'update', ended)


async def test_admission_duplicate_cooldown_and_inactivity(setup):
    now = [100]
    manager = SessionManager(setup.settings, setup, clock=lambda: now[0])
    manager.submit(event('first'))
    manager.submit(event('first'))
    manager.submit(event('second'))
    await manager.task
    assert setup.metrics['sessions_started'] == 1
    now[0] += 60
    manager.rearm()
    assert manager.armed  # Explicit inactivity heuristic, not a proof of absence.
    manager.submit(event('first', True))
    manager.submit(event('second', True))
    now[0] += 11
    manager.submit(event('third'))
    await manager.task
    assert setup.metrics['sessions_started'] == 2


async def test_detection_end_does_not_imply_departure_but_explicit_cancel_works(setup):
    c = setup
    started = asyncio.Event()
    async def stall(*args):
        started.set()
        await asyncio.Event().wait()
    c.provider.vision.side_effect = stall
    manager = SessionManager(c.settings, c)
    manager.submit(event('person'))
    await started.wait()
    manager.submit(event('person', True))
    await asyncio.sleep(0)
    assert not manager.task.done()
    assert c.metrics['detection_end_ignored'] == 1
    await manager.cancel()
    assert not c.history and c.state == State.COOLDOWN


@pytest.mark.parametrize('text', ['', 'word ' * 26, 'Your name?', 'You are an idiot', 'One? Two?'])
def test_output_contract_rejects_unsafe_or_long(text):
    with pytest.raises(ValueError):
        Reply(response_text=text, tone='friendly', ends_conversation=False)


def test_audio_gate_discards_and_clears():
    mic = LiveMicrophone('rtsp://u:p@camera/stream')
    mic.feed(bytes(FRAME_BYTES))
    assert mic.frames.empty()
    mic.healthy = True
    mic.open_gate()
    mic.feed(bytes(FRAME_BYTES))
    assert mic.frames.qsize() == 1
    mic.close_gate()
    assert mic.frames.empty()
    mic.healthy = False
    with pytest.raises(MicrophoneFailure):
        mic.open_gate()


async def test_vad_onset_silence_and_transport_failure():
    vad = Obj(is_speech=Mock(side_effect=[True] * 7 + [False] * 24))
    mic = LiveMicrophone('rtsp://u:p@camera/stream', vad=vad)
    mic.healthy = True
    mic.open_gate()
    for _ in range(31):
        mic.feed(bytes(FRAME_BYTES))
    pcm = await mic.listen(1)
    assert len(pcm) == 31 * FRAME_BYTES
    assert not mic.gate
    mic.healthy = False
    with pytest.raises(MicrophoneFailure):
        await mic.listen(1)


async def test_mic_eof_reconnects_and_reaps():
    process = Obj(stdout=Obj(readexactly=AsyncMock(side_effect=asyncio.IncompleteReadError(b'', FRAME_BYTES))),
                  returncode=None, kill=Mock(), wait=AsyncMock())
    mic = LiveMicrophone('rtsp://u:p@camera/stream', spawn=AsyncMock(return_value=process))
    mic.start()
    for _ in range(10):
        await asyncio.sleep(0)
        if mic.reconnects:
            break
    assert mic.reconnects == 1 and not mic.healthy
    await mic.close()
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_websocket_disconnect_terminates_subscription():
    from uiprotect.websocket import WebsocketState
    unsubscribe = Mock()
    unsub_state = Mock()
    def state_subscribe(callback):
        callback(WebsocketState.DISCONNECTED)
        return unsub_state
    client = Obj(subscribe_websocket=Mock(return_value=unsubscribe),
                 subscribe_websocket_state=state_subscribe)
    adapter = UiprotectAdapter(client, 'camera')
    with pytest.raises(ConnectionError):
        async for _ in adapter.events(1):
            pass
    unsubscribe.assert_called_once()
    unsub_state.assert_called_once()


async def test_third_reply_deadline_cannot_open_fourth_listen(setup):
    c = setup
    c.settings.max_session_seconds = 0.03
    original = c.provider.reply.return_value
    async def reply(costume, history, final):
        if final:
            await asyncio.Event().wait()
        return original
    c.provider.reply.side_effect = reply
    await c.run()
    assert c.last_result == 'deadline'
    assert c.microphone.listen_count == 3
    assert not c.microphone.gate and not c.history


async def test_echo_guard_precedes_open_gate(setup):
    c = setup
    c.settings.audio_echo_guard_ms = 10
    c.microphone.utterances = [None]
    last_play = [None]
    async def play(path):
        assert not c.microphone.gate
        last_play[0] = asyncio.get_running_loop().time()
    c.protect.play.side_effect = play
    open_gate = c.microphone.open_gate
    def checked_open():
        assert asyncio.get_running_loop().time() - last_play[0] >= 0.009
        open_gate()
    c.microphone.open_gate = checked_open
    await c.run()
    assert c.microphone.listen_count == 1


async def test_bounded_pcm_queue_overflow_fails_closed():
    mic = LiveMicrophone('rtsp://u:p@camera/stream')
    mic.healthy = True
    mic.open_gate()
    for _ in range(100):
        mic.feed(bytes(FRAME_BYTES))
    with pytest.raises(MicrophoneFailure):
        mic.feed(bytes(FRAME_BYTES))
    assert not mic.gate and not mic.healthy and mic.frames.empty()


async def test_real_vad_silence_does_not_become_speech():
    mic = LiveMicrophone('rtsp://u:p@camera/stream')
    mic.healthy = True
    mic.open_gate()
    mic.feed(bytes(FRAME_BYTES))
    assert await mic.listen(0.01) is None
