"""Behaviour of patch_voice_pill on pristine 8.12.0 sources, patched as apply.sh does.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_voice_pill_patch.py

voice_mode's server modules are heavy to import, so tests lift the patched
functions out of the patched source and run them against stubs.  The code under
test is the real patched text; only its collaborators are fake.
"""
import ast
import asyncio
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import typing
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import numpy as np
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
FILES = ("control_channel.py", "control_socket.py", "core.py", "converse.py", "streaming.py")
PILL_FILES = ("control_channel.py", "control_socket.py", "converse.py", "streaming.py")
PLAYERS = {
    "core.py": ["_wait_for_player_with_control"],
    "converse.py": ["_play_samples_controllable"],
    "streaming.py": ["stream_pcm_audio", "stream_cartesia_pcm", "stream_with_buffering"],
}
ALL_PLAYERS = {name for names in PLAYERS.values() for name in names}
STOP_MESSAGE = "Voice was stopped from the pill. Continue in text unless the user asks for voice again."


def _patch(script, target):
    r = subprocess.run([sys.executable, str(PATCHES / script), str(target)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"{script} on {target.name}: {r.stderr}"
    return r.stdout


@pytest.fixture(scope="module", autouse=True)
def throwaway_home(tmp_path_factory):
    """Nothing in this file may reach the real ~/.voicemode, including module-scope setup."""
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    d = tmp_path_factory.mktemp("pill")
    for f in FILES:
        shutil.copy(Path(raw) / f, d / f)
    # Same order as apply.sh.
    for script in ("patch_listen_overrun.py", "patch_turns_playback_stop.py", "patch_session_name.py"):
        _patch(script, d / "converse.py")
    _patch("patch_control_hold.py", d / "control_channel.py")
    _patch("patch_converse_hold.py", d / "converse.py")
    for f in ("core.py", "converse.py", "streaming.py"):
        _patch("patch_hold_barges_in.py", d / f)
    _patch("patch_voice_language.py", d / "converse.py")
    for f in ("control_channel.py", "control_socket.py", "core.py", "converse.py"):
        _patch("patch_voice_barge.py", d / f)
    for f in PILL_FILES:
        _patch("patch_voice_pill.py", d / f)
    return d


@pytest.fixture()
def control(work):
    spec = importlib.util.spec_from_file_location("cc_pill", work / "control_channel.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cc_pill"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def st(monkeypatch, tmp_path):
    """The real state module, with its default base under a throwaway HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("vps_pill", PATCHES / "voice_pill_state.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(work, fname):
    return ast.parse((work / fname).read_text())


def _named(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name)


def _lift(work, fname, names, ns):
    """Run the named top-level definitions from the patched file inside ns."""
    tree = _tree(work, fname)
    mod = ast.Module(body=[_named(tree, n) for n in names], type_ignores=[])
    ns.setdefault("__name__", "lifted")
    exec(compile(mod, fname, "exec"), ns)
    return ns


def _base_ns(st):
    return {"voice_pill_state": st, "os": os, "logger": logging.getLogger("pill-test"),
            "TTS_VOICES": ["af_sky"], "_vml_session_name": lambda: "lbl",
            "Optional": typing.Optional, "Dict": typing.Dict, "Tuple": typing.Tuple,
            "np": np, "threading": threading, "time": time, "asyncio": asyncio}


@pytest.fixture()
def helpers(work, st):
    return _lift(work, "converse.py",
                 ["_voice_pill_enter", "_voice_pill_voice", "_voice_pill_result"], _base_ns(st))


# -- listening ---------------------------------------------------------------

def test_status_reports_listening(work, control):
    assert '"listening": control_state.is_listening,' in (work / "control_socket.py").read_text()
    state = control.ControlState()
    assert state.is_listening is False
    state.set_listening(True)
    assert state.is_listening is True
    state.set_listening(False)
    assert state.is_listening is False


def test_listening_false_after_recording(work):
    tree = _tree(work, "converse.py")
    src = ast.unparse(_named(tree, "record_audio_with_silence_detection"))
    assert "set_listening(True)" in src
    assert "finally" in src and "set_listening(False)" in src
    assert "record_audio_with_silence_detection_inner(" in src
    _named(tree, "record_audio_with_silence_detection_inner")


def test_listening_clears_when_recording_raises(work, st, control):
    state = control.ControlState()
    seen = []

    def inner(*a):
        seen.append(state.is_listening)
        raise RuntimeError("no device")

    ns = _base_ns(st)
    ns.update(get_control_state=lambda: state, _voice_pill_discard=threading.Event(),
              record_audio_with_silence_detection_inner=inner)
    _lift(work, "converse.py", ["record_audio_with_silence_detection"], ns)
    with pytest.raises(RuntimeError):
        ns["record_audio_with_silence_detection"](1.0)
    assert seen == [True]
    assert state.is_listening is False


# -- speaking on the streaming path ------------------------------------------

@pytest.mark.parametrize("func", PLAYERS["streaming.py"])
def test_streaming_players_report_speaking(work, st, control, func):
    state = control.ControlState()
    seen = []

    async def inner(*a, **k):
        seen.append((state.is_speaking, a, k))
        if k.get("boom"):
            raise RuntimeError("stream died")
        return True, "metrics"

    ns = _base_ns(st)
    ns.update(get_control_state=lambda: state, StreamMetrics=object, **{f"{func}_inner": inner})
    _lift(work, "streaming.py", [func], ns)

    assert asyncio.run(ns[func]("text", 1, x=2)) == (True, "metrics")
    assert seen == [(True, ("text", 1), {"x": 2})]
    assert state.is_speaking is False
    with pytest.raises(RuntimeError):
        asyncio.run(ns[func]("text", boom=True))
    assert state.is_speaking is False


def _reaches(tree, start):
    """Every module-level function reachable by name from start."""
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seen, todo = set(), [start]
    while todo:
        for call in ast.walk(funcs[todo.pop()]):
            if isinstance(call, ast.Name) and call.id in funcs and call.id not in seen:
                seen.add(call.id)
                todo.append(call.id)
    return seen


@pytest.mark.parametrize("fname,func", [(f, n) for f, names in PLAYERS.items() for n in names])
def test_players_do_not_nest(work, fname, func):
    """A wrapped player calling another would turn speaking off mid-utterance."""
    reached = _reaches(_tree(work, fname), f"{func}_inner")
    assert not (reached & ALL_PLAYERS), f"{func} reaches {reached & ALL_PLAYERS}"


# -- entering a turn ---------------------------------------------------------

def test_enter_registers_and_touches_the_session(helpers, st):
    assert asyncio.run(helpers["_voice_pill_enter"]("am_echo")) is None
    [s] = st.live_sessions()
    assert (s["pid"], s["label"], s["voice"]) == (os.getpid(), "lbl", "am_echo")
    assert s["last_voice_at"] is not None
    started = s["started_at"]
    assert asyncio.run(helpers["_voice_pill_enter"](None)) is None
    [s] = st.live_sessions()
    assert (s["started_at"], s["voice"]) == (started, "af_sky")  # default when no voice given


def test_enter_returns_stop_message_once(helpers, st):
    asyncio.run(helpers["_voice_pill_enter"]("v"))
    st.request_stop(os.getpid())
    assert asyncio.run(helpers["_voice_pill_enter"]("v")) == STOP_MESSAGE
    assert [s["stopped"] for s in st.live_sessions()] == [True]  # listed, so the pill stays
    assert asyncio.run(helpers["_voice_pill_enter"]("v")) is None
    assert [s["stopped"] for s in st.live_sessions()] == [False]  # a new turn re-engages it


def test_stop_message_reaches_the_agent_exactly_once(helpers, st):
    asyncio.run(helpers["_voice_pill_enter"]("v"))
    st.request_stop(os.getpid())
    early = asyncio.run(helpers["_voice_pill_enter"]("v"))  # stopped before the turn began
    assert helpers["_voice_pill_result"](early).count(STOP_MESSAGE) == 1
    cut = helpers["_voice_pill_result"]("[control: stop] playback stopped")  # cut mid-turn
    assert cut.count(STOP_MESSAGE) == 1


def test_enter_waits_while_paused_then_continues(helpers, st):
    st.set_paused(True, os.getpid())
    threading.Timer(0.3, st.set_paused, (False, os.getpid())).start()
    t0 = time.monotonic()
    assert asyncio.run(helpers["_voice_pill_enter"]("v")) is None
    assert time.monotonic() - t0 >= 0.25


def test_enter_wait_can_be_cancelled(helpers, st):
    st.set_paused(True, os.getpid())

    async def go():
        await asyncio.wait_for(helpers["_voice_pill_enter"]("v"), 0.1)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(go())


def test_enter_ignores_a_pause_whose_owner_died(helpers, st):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    st.set_paused(True, p.pid)
    p.wait()
    assert asyncio.run(asyncio.wait_for(helpers["_voice_pill_enter"]("v"), 2)) is None


def test_broken_state_cannot_cost_a_turn(work, st, monkeypatch):
    ns = _lift(work, "converse.py", ["_voice_pill_enter", "_voice_pill_voice", "_voice_pill_result"], _base_ns(st))

    def boom(*a, **k):
        raise OSError("disk full")

    for attr in ("register_session", "touch_session", "get_override", "take_stop"):
        monkeypatch.setattr(st, attr, boom)
    assert asyncio.run(ns["_voice_pill_enter"]("v")) is None
    assert ns["_voice_pill_voice"]("v") == "v"
    assert ns["_voice_pill_result"]("r") == "r"


def test_is_paused_reads_false_when_the_base_is_unusable(st, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert st.is_paused(base=blocker / "pill") is False


def test_enter_runs_before_the_conch_and_replaces_voice(work):
    src = ast.unparse(_named(_tree(work, "converse.py"), "_converse_core"))
    enter = src.index("_voice_pill_enter(voice)")
    replace = src.index("voice = _voice_pill_voice(voice)")
    assert enter < replace < src.index("resolved_voice =") < src.index("conch.try_acquire()")


def test_override_replaces_voice(helpers, st):
    assert helpers["_voice_pill_voice"]("caller") == "caller"
    st.set_override(os.getpid(), "bm_george", None)
    assert helpers["_voice_pill_voice"]("caller") == "bm_george"
    assert helpers["_voice_pill_voice"](None) == "bm_george"


def test_override_for_another_session_is_ignored(helpers, st):
    st.set_override(os.getpid() + 1, "bm_george", "German")
    assert helpers["_voice_pill_voice"]("caller") == "caller"
    assert helpers["_voice_pill_result"]("said") == "said"


def test_language_override_is_added_to_result(helpers, st):
    assert helpers["_voice_pill_result"]("said") == "said"
    st.set_override(os.getpid(), None, "German")
    out = helpers["_voice_pill_result"]("said")
    assert out.startswith("said")
    assert out.endswith("The user selected German. Write your next messages in German.")


def test_language_note_wraps_the_tool_result(work):
    src = ast.unparse(_named(_tree(work, "converse.py"), "converse"))
    assert "_voice_pill_result(result + _build_widgets_segment(effective_time_in_response))" in src


def test_dead_session_leaves_no_override_or_stop_behind(st):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    st.register_session(p.pid, "x", "v")
    st.set_override(p.pid, "v2", None)
    st.request_stop(p.pid)
    p.kill()
    p.wait()
    assert st.live_sessions() == []
    assert st.get_override(p.pid) == {}
    assert st.take_stop(p.pid) is False


# -- pause while listening ---------------------------------------------------

class _FakeStream:
    """Delivers two chunks at once; the fake VAD pauses on the first."""

    def __init__(self, callback=None, blocksize=None, **_):
        self.callback, self.blocksize = callback, blocksize

    def __enter__(self):
        for _ in range(2):
            self.callback(np.ones((self.blocksize, 1), dtype=np.int16), self.blocksize, None, None)
        return self

    def __exit__(self, *a):
        return False


def _record_ns(work, st, control, on_chunk):
    # The recording loop imports scipy.signal lazily on its first chunk. Cold, that import can take
    # tens of seconds on a loaded machine and ate the tests' time budget (Mac, 3 of 20 runs under
    # CPU load: 21-26 s inside the import). Import it here, before any timer starts.
    import scipy.signal  # noqa: F401
    state = control.ControlState()
    ns = _base_ns(st)
    ns.update(
        get_control_state=lambda: state, COMMAND_SKIP_BACK=control.COMMAND_SKIP_BACK,
        VAD_AVAILABLE=True, DISABLE_SILENCE_DETECTION=False, VAD_DEBUG=False, DEBUG=False,
        VAD_AGGRESSIVENESS=2, SAMPLE_RATE=24000, CHANNELS=1, VAD_CHUNK_DURATION_MS=30,
        SILENCE_THRESHOLD_MS=1000, MIN_RECORDING_DURATION=0.5, INITIAL_SILENCE_GRACE_PERIOD=0.5,
        sd=types.SimpleNamespace(InputStream=_FakeStream, PortAudioError=OSError),
        webrtcvad=types.SimpleNamespace(Vad=lambda _: types.SimpleNamespace(is_speech=on_chunk)),
        _voice_pill_discard=threading.Event(), _voice_barge_pending=threading.Event(),
    )
    _lift(work, "converse.py",
          ["record_audio_with_silence_detection", "record_audio_with_silence_detection_inner"], ns)
    return ns, state


def test_pause_ends_the_recording_loop_and_flags_it(work, st, control):
    calls = []

    def on_chunk(_bytes, _rate):
        calls.append(1)
        st.set_paused(True, os.getpid())
        return True

    ns, state = _record_ns(work, st, control, on_chunk)
    t0 = time.monotonic()
    ns["record_audio_with_silence_detection"](30.0)
    assert time.monotonic() - t0 < 5
    assert len(calls) == 1  # the second queued chunk was never processed
    assert ns["_voice_pill_discard"].is_set()
    assert state.is_listening is False


def test_stop_dominates_pause_in_the_recording_loop(work, st, control):
    def on_chunk(_bytes, _rate):
        st.set_paused(True, os.getpid())
        state.request_stop()
        return True

    ns, state = _record_ns(work, st, control, on_chunk)
    ns["record_audio_with_silence_detection"](30.0)
    assert not ns["_voice_pill_discard"].is_set()


@dataclass
class _Rec:
    audio: list = field(default_factory=list)
    stt: list = field(default_factory=list)


def _listen_ns(work, st, control, monkeypatch, record, state=None):
    rec = _Rec()

    async def chime(*a, **k):
        rec.audio.append("chime")

    async def stt(audio, *a):
        rec.stt.append(audio.tolist())
        return {"text": "hello", "provider": "fake"}

    ns = _base_ns(st)
    ns.update(
        play_audio_feedback=chime, speech_to_text=stt, openai_clients=None,
        record_audio_with_silence_detection=record, SAVE_AUDIO=False, AUDIO_DIR=None,
        COMMAND_SKIP_BACK=control.COMMAND_SKIP_BACK, SAMPLE_RATE=24000,
        _voice_pill_discard=threading.Event(), _voice_barge_pending=threading.Event(),
    )
    exec("from dataclasses import dataclass, field\nfrom typing import Any, List", ns)
    _lift(work, "converse.py", ["ListenResult", "listen_and_transcribe"], ns)
    fake_pkg = types.ModuleType("voice_mode")
    fake_barge = types.ModuleType("voice_mode.voice_barge")
    fake_barge.prepend_preroll = lambda audio, *_: audio
    monkeypatch.setitem(sys.modules, "voice_mode", fake_pkg)
    monkeypatch.setitem(sys.modules, "voice_mode.voice_barge", fake_barge)
    return ns, rec


def _listen(ns, state):
    return asyncio.run(ns["listen_and_transcribe"](
        state, listen_duration_max=5.0, listen_duration_min=0.0,
        disable_silence_detection=False, vad_aggressiveness=None, chime_enabled=False,
        chime_leading_silence=None, chime_trailing_silence=None, transport="local"))


def test_recording_during_pause_is_discarded(work, st, control, monkeypatch):
    state = control.ControlState()
    takes = [np.array([1, 2, 3]), np.array([9])]
    opened_while_paused = []

    def record(*a):
        opened_while_paused.append(st.is_paused())
        audio = takes.pop(0)
        if takes:  # first take: paused mid-recording, resumed a moment later
            st.set_paused(True, os.getpid())
            ns["_voice_pill_discard"].set()
            threading.Timer(0.3, st.set_paused, (False, os.getpid())).start()
        return audio, True

    ns, rec = _listen_ns(work, st, control, monkeypatch, record)
    result = _listen(ns, state)
    assert rec.stt == [[9]]  # the first take never reached speech-to-text
    assert result.outcome == "answered" and result.text == "hello"
    assert opened_while_paused == [False, False]  # the mic stayed shut until resume
    assert not ns["_voice_pill_discard"].is_set()


def test_flagged_recording_is_discarded_even_after_a_quick_resume(work, st, control, monkeypatch):
    state = control.ControlState()
    takes = [np.array([1]), np.array([9])]

    def record(*a):
        audio = takes.pop(0)
        if takes:  # the pause was already over by the time this returned
            ns["_voice_pill_discard"].set()
        return audio, True

    ns, rec = _listen_ns(work, st, control, monkeypatch, record)
    _listen(ns, state)
    assert rec.stt == [[9]]


def test_stop_during_pause_ends_the_listen(work, st, control, monkeypatch):
    state = control.ControlState()

    def record(*a):
        st.set_paused(True, os.getpid())
        ns["_voice_pill_discard"].set()
        threading.Timer(0.3, state.request_stop).start()
        return np.array([1]), True

    ns, rec = _listen_ns(work, st, control, monkeypatch, record)
    result = _listen(ns, state)
    assert result.outcome == "stopped"
    assert rec.stt == []


def test_pause_does_not_disturb_a_voice_cut(work, st, control, monkeypatch):
    """After a barged take is discarded, the relisten is an ordinary one."""
    state = control.ControlState()
    state.set_speaking(True)
    state.request_barge_in()
    state.set_speaking(False)
    state.reset(preserve_hold=True)
    takes = [np.array([1]), np.array([9])]
    pending_seen = []

    def record(*a):
        pending_seen.append(ns["_voice_barge_pending"].is_set())
        audio = takes.pop(0)
        if takes:
            ns["_voice_pill_discard"].set()
        return audio, True

    ns, rec = _listen_ns(work, st, control, monkeypatch, record)
    _listen(ns, state)
    assert pending_seen == [True, False]
    assert not ns["_voice_barge_pending"].is_set()
    assert rec.stt == [[9]]


class _FakeRec:
    """sd.rec/get_stream/stop/wait. The first take is paused on its first poll and
    resumed moments later; without polling, Pause and resume both land inside wait()."""

    def __init__(self, st):
        self.st, self.takes, self.stopped, self.waits = st, 0, 0, 0

    def _pause_then_resume(self):
        self.st.set_paused(True, os.getpid())
        threading.Timer(0.2, self.st.set_paused, (False, os.getpid())).start()

    def rec(self, frames, **_):
        self.takes += 1
        return np.full((2, 1), self.takes, dtype=np.int16)

    def get_stream(self):
        return self

    @property
    def active(self):
        if self.takes == 1 and not self.stopped:
            self._pause_then_resume()
            return True
        return False

    def stop(self):
        self.stopped += 1

    def wait(self):
        self.waits += 1
        if self.takes == 1:  # the old sd.wait(): blocks through the whole Pause
            self.st.set_paused(True, os.getpid())
            self.st.set_paused(False, os.getpid())


def _fixed_ns(work, st, control, monkeypatch, sd, state):
    ns, rec = _listen_ns(work, st, control, monkeypatch, None)
    ns.update(sd=sd, DEBUG=False, CHANNELS=1, VAD_AVAILABLE=False, DISABLE_SILENCE_DETECTION=False,
              get_control_state=lambda: state)
    _lift(work, "converse.py", ["record_audio", "record_audio_with_silence_detection",
                                "record_audio_with_silence_detection_inner"], ns)
    return ns, rec


def test_fixed_duration_recording_under_pause_never_reaches_stt(work, st, control, monkeypatch):
    state = control.ControlState()
    sd = _FakeRec(st)
    ns, rec = _fixed_ns(work, st, control, monkeypatch, sd, state)
    result = _listen(ns, state)
    assert rec.stt == [[2, 2]]  # only the take after resume
    assert result.outcome == "answered"
    assert sd.stopped == 1  # the mic was closed at once


def test_pause_closes_the_fixed_duration_mic(work, st, control, monkeypatch):
    state = control.ControlState()
    sd = _FakeRec(st)
    ns, _ = _fixed_ns(work, st, control, monkeypatch, sd, state)
    out, _speech = ns["record_audio_with_silence_detection"](5.0)
    assert len(out) == 0
    assert sd.stopped == 1
    assert sd.waits == 0
    assert ns["_voice_pill_discard"].is_set()
    assert state.is_listening is False


def test_fixed_duration_without_pause_is_unchanged(work, st, control, monkeypatch):
    state = control.ControlState()
    sd = _FakeRec(st)
    sd.takes = 1  # rec() makes this take 2, which never pauses
    ns, _ = _fixed_ns(work, st, control, monkeypatch, sd, state)
    out, speech = ns["record_audio_with_silence_detection"](5.0)
    assert out.tolist() == [2, 2] and speech is True
    assert (sd.stopped, sd.waits) == (0, 1)  # wait() still runs, it closes the stream
    assert not ns["_voice_pill_discard"].is_set()


def test_queued_session_does_not_start_during_pause(work, st, control):
    """A session past enter, queued for the conch, gets the floor mid-pause."""
    state = control.ControlState()
    events = []
    ns = _base_ns(st)
    ns.update(get_control_state=lambda: state, asynccontextmanager=asynccontextmanager,
              start_control_listener=lambda: events.append("start"),
              stop_control_listener=lambda: events.append("stop"))
    _lift(work, "converse.py", ["_control_listener_scope"], ns)
    st.set_paused(True, os.getpid())
    threading.Timer(0.3, st.set_paused, (False, os.getpid())).start()

    async def go():
        async with ns["_control_listener_scope"]():
            events.append(("body", st.is_paused()))

    asyncio.run(go())
    assert events == ["start", ("body", False), "stop"]


# -- voice_stop --------------------------------------------------------------

def test_voice_stop_removes_this_session(st, monkeypatch):
    tools = []
    server = types.ModuleType("voice_mode.server")
    server.mcp = types.SimpleNamespace(tool=lambda: (lambda f: tools.append(f) or f))
    barge = types.ModuleType("voice_mode.voice_barge")
    barge.barge_control = lambda a: a
    pkg = types.ModuleType("voice_mode")
    pkg.voice_pill_state = st
    for name, mod in (("voice_mode", pkg), ("voice_mode.server", server),
                      ("voice_mode.voice_barge", barge), ("voice_mode.voice_pill_state", st)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("vbt_pill", PATCHES / "voice_barge_tool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert "stop voice mode" in mod.voice_stop.__doc__
    assert mod.voice_stop in tools
    st.register_session(os.getpid(), "x", "v")
    asyncio.run(mod.voice_stop())
    assert st.live_sessions() == []


# -- patch mechanics ---------------------------------------------------------

def test_patch_is_idempotent(work):
    for f in PILL_FILES:
        before = (work / f).read_text()
        assert "already patched" in _patch("patch_voice_pill.py", work / f)
        assert (work / f).read_text() == before


def test_anchor_drift_fails_loudly(tmp_path):
    target = tmp_path / "converse.py"
    target.write_text("# nothing to anchor on\n")
    r = subprocess.run([sys.executable, str(PATCHES / "patch_voice_pill.py"), str(target)],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "ANCHOR DRIFT" in r.stderr


def test_apply_sh_runs_it_after_barge_on_every_target():
    text = (PATCHES / "apply.sh").read_text()
    barge = text.index('"$SCRIPT_DIR/patch_voice_barge.py" "$VM_DIR/tools/converse.py"')
    for target in ("control_channel.py", "control_socket.py", "tools/converse.py", "streaming.py"):
        assert text.index(f'"$SCRIPT_DIR/patch_voice_pill.py" "$VM_DIR/{target}"') > barge
