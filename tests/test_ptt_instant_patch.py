"""patches/patch_ptt_instant.py: push-to-talk acts the moment the key goes down.

Robert, 2026-11-07: a hold_start at ANY point of a voice turn -- TTS generation,
playback, listening -- cuts the reply and records from then on; a stop (the tap)
clears the hold and drops the recording untranscribed.

The patched pieces run here against fakes: the ControlState from a fully patched tree
(apply.py with the real manifest), and converse's helper and listen_and_transcribe
lifted out of the patched converse.py with stand-ins for audio, chimes and STT.
No sound, no devices, no voice_mode import.

Run: VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_ptt_instant_patch.py
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
import typing
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
MARKER = "voicemode-local ptt instant"
SOURCES = {"control_channel.py": "control_channel.py", "control_socket.py": "control_socket.py",
           "core.py": "core.py", "simple_failover.py": "simple_failover.py",
           "streaming.py": "streaming.py", "server.py": "server.py", "tools/converse.py": "converse.py",
           "conch_notify.py": "conch_notify.py", "conch_queue.py": "conch_queue.py", "conch.py": "conch.py"}


def _src() -> Path:
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    return Path(raw)


def _tree(root: Path, manifest: str = "manifest.json", crlf: bool = False) -> Path:
    pkg = root / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    for rel, name in SOURCES.items():
        data = (_src() / name).read_bytes()
        (pkg / rel).write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)
    r = subprocess.run([sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(pkg),
                        "--manifest", str(PATCHES / manifest)],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    return pkg


@pytest.fixture(scope="module")
def pkg(tmp_path_factory):
    return _tree(tmp_path_factory.mktemp("instant"))


@pytest.fixture(scope="module")
def control(pkg):
    spec = importlib.util.spec_from_file_location("vml_instant_control", pkg / "control_channel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def converse_src(pkg):
    return (pkg / "tools" / "converse.py").read_text(encoding="utf-8")


def _lift(source: str, name: str, namespace: dict):
    """Exec one top-level function (plus module constants it names) from source."""
    tree = ast.parse(source)
    wanted = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert wanted, f"{name} not found"
    consts = [n for n in tree.body if isinstance(n, ast.Assign)
              and all(isinstance(t, ast.Name) and t.id.startswith("_PTT_") for t in n.targets)]
    exec(compile(ast.Module(body=consts + wanted, type_ignores=[]), "<lifted>", "exec"), namespace)
    return namespace[name]


# --- control state -----------------------------------------------------------

def test_stop_clears_a_latched_hold(control):
    st = control.ControlState()
    st.request_hold_start()
    st.request_stop(hint="switch-to-text")
    snap = st.snapshot()
    assert snap.is_stopped and snap.is_holding is False and snap.hint == "switch-to-text"


def test_hold_after_stop_is_still_refused(control):
    st = control.ControlState()
    st.request_stop()
    assert st.request_hold_start() is False and st.snapshot().is_holding is False


def test_status_reports_the_hold(pkg):
    src = (pkg / "control_socket.py").read_text(encoding="utf-8")
    assert '"holding": getattr(snap, "is_holding", False),' in src


# --- TTS generation is cut by a hold or a stop --------------------------------

@pytest.fixture
def cuttable(converse_src):
    ns = {"asyncio": asyncio, "time": time, "logger": logging.getLogger("test.instant")}
    return _lift(converse_src, "_ptt_cuttable_tts", ns)


def _run(coro):
    return asyncio.run(coro)


def test_without_a_press_the_reply_is_untouched(control, cuttable):
    st = control.ControlState()

    async def tts(**kw):
        await asyncio.sleep(0.05)
        return True, {"ttfa": 0.01, "generation": 0.04, "playback": 0.01}, {"provider": "kokoro", **kw}

    assert _run(cuttable(st, tts, message="hi")) == (
        True, {"ttfa": 0.01, "generation": 0.04, "playback": 0.01}, {"provider": "kokoro", "message": "hi"})


@pytest.mark.parametrize("press", ["hold", "stop"])
def test_a_press_during_generation_cuts_it_at_once(control, cuttable, press):
    """Kokoro's ttfa was 7.9 s on 2026-11-07; nothing polled the control state then."""
    st = control.ControlState()
    seen = {}

    async def generating(**kw):
        try:
            await asyncio.sleep(10)  # waiting for the TTS server
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        return True, {}, {}

    async def main():
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, st.request_hold_start if press == "hold" else st.request_stop)
        start = time.perf_counter()
        result = await cuttable(st, generating, voice="af_sky")
        return result, time.perf_counter() - start

    (ok, metrics, config), took = _run(main())
    assert ok is True and seen == {"cancelled": True}
    assert took < 0.6, took  # 0.1 s to the press, then one 50 ms poll
    assert metrics["ptt_cut"] is True and metrics["playback"] == 0.0
    assert config["provider"] == "ptt-cut" and config["voice"] == "af_sky"
    snap = st.snapshot()
    assert snap.is_holding if press == "hold" else snap.is_stopped  # latched for what follows


def test_a_hold_during_playback_lets_the_player_cut_itself(control, cuttable):
    """While speaking, the playback loops abort on is_holding themselves
    (patch_hold_barges_in), stopping the device. The helper must not cancel them
    first, or a cancelled wait could leave the player running."""
    st = control.ControlState()
    seen = {}

    async def playing(**kw):
        st.set_speaking(True)
        try:
            while not st.snapshot().is_holding:
                await asyncio.sleep(0.05)
            seen["player_stopped"] = True
            return True, {"ttfa": 0.1, "generation": 0.1, "playback": 0.2, "control_stopped": True}, {"provider": "kokoro"}
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        finally:
            st.set_speaking(False)

    async def main():
        asyncio.get_running_loop().call_later(0.15, st.request_hold_start)
        return await cuttable(st, playing)

    ok, metrics, config = _run(main())
    assert seen == {"player_stopped": True} and config["provider"] == "kokoro"
    assert st.snapshot().is_holding  # still latched: the recording comes next


def test_a_cancelled_converse_cancels_the_tts(control, cuttable):
    st = control.ControlState()
    seen = {}

    async def generating(**kw):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def main():
        task = asyncio.ensure_future(cuttable(st, generating))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

    _run(main())
    assert seen == {"cancelled": True}


def test_the_main_speak_call_goes_through_the_helper(converse_src):
    assert converse_src.count("await _ptt_cuttable_tts(control_state, text_to_speech_with_failover,") == 1


# --- listening: a held key records at once; a stop drops the recording -------

class _Fakes:
    def __init__(self, st, during_record=None):
        self.st, self.during_record = st, during_record
        self.chimes, self.stt_calls, self.record_saw_hold = [], 0, None

    async def play_audio_feedback(self, kind, clients, enabled, *a, **k):
        self.chimes.append((kind, enabled))

    def record(self, *a):
        self.record_saw_hold = self.st.snapshot().is_holding
        if self.during_record:
            self.during_record(self.st)
        import numpy as np
        return np.zeros(2400, dtype="float32"), True

    async def speech_to_text(self, *a, **k):
        self.stt_calls += 1
        return {"text": "should not happen"}


def _listen(converse_src, st, fakes):
    async def wait_while_paused(until=None):
        return None

    ns = {
        "asyncio": asyncio, "time": time, "threading": threading, "logger": logging.getLogger("t"),
        "voice_pill_state": SimpleNamespace(wait_while_paused=wait_while_paused, is_paused=lambda: False),
        "play_audio_feedback": fakes.play_audio_feedback, "openai_clients": {},
        "record_audio_with_silence_detection": fakes.record, "speech_to_text": fakes.speech_to_text,
        "ListenResult": lambda **kw: SimpleNamespace(**kw), "SAMPLE_RATE": 24000,
        "_voice_barge_pending": threading.Event(), "_voice_pill_discard": threading.Event(),
        "COMMAND_SKIP_BACK": "skip_back", "SAVE_AUDIO": False, "AUDIO_DIR": None, "Dict": dict,
        "Optional": typing.Optional,
    }
    fn = _lift(converse_src, "listen_and_transcribe", ns)
    return fn(st, listen_duration_max=30, listen_duration_min=0, disable_silence_detection=False,
              vad_aggressiveness=None, chime_enabled=True, chime_leading_silence=None,
              chime_trailing_silence=None, transport="local", pre_listen_pause=5.0)


def test_a_held_key_records_at_once_and_a_tap_drops_it(control, converse_src):
    """The tap: hold_start on key-down (recording begins, no pause, no chime), stop on
    key-up within 0.8 s -- the hold is cleared and nothing reaches speech-to-text."""
    st = control.ControlState()
    st.request_hold_start()
    fakes = _Fakes(st, during_record=lambda s: s.request_stop(hint="switch-to-text"))
    start = time.perf_counter()
    result = asyncio.run(_listen(converse_src, st, fakes))
    assert time.perf_counter() - start < 1.0  # the 5 s pre-listen pause was skipped
    assert fakes.record_saw_hold is True
    assert ("listening", True) not in fakes.chimes  # no "listening" chime: they are talking already
    assert result.outcome == "stopped" and fakes.stt_calls == 0
    assert result.control.is_holding is False and result.control.hint == "switch-to-text"


def test_without_a_hold_the_listen_is_unchanged(control, converse_src):
    st = control.ControlState()
    fakes = _Fakes(st, during_record=lambda s: s.request_stop())
    ns_start = time.perf_counter()
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(_listen(converse_src, st, fakes))
        loop.run_until_complete(asyncio.sleep(0.3))
        assert not task.done()  # still in the 5 s pre-listen pause
        task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    finally:
        loop.close()
    assert time.perf_counter() - ns_start < 2


# --- patch hygiene ---------------------------------------------------------

def test_marker_in_every_target(pkg):
    for rel in ("control_channel.py", "control_socket.py", "tools/converse.py"):
        assert MARKER in (pkg / rel).read_text(encoding="utf-8"), rel


@pytest.mark.parametrize("manifest", ["manifest.json", "manifest.windows.json"])
def test_both_manifests_apply_it_after_the_pill(manifest):
    import json
    steps = json.loads((PATCHES / manifest).read_text(encoding="utf-8"))["steps"]
    names = [s.get("patch") for s in steps]
    assert "patch_ptt_instant.py" in names
    assert names.index("patch_ptt_instant.py") > names.index("patch_voice_pill.py")


def test_idempotent_and_crlf_safe(tmp_path):
    pkg = _tree(tmp_path / "a", crlf=True)
    before = {p: p.read_bytes() for p in pkg.rglob("*.py")}
    for rel in ("control_channel.py", "control_socket.py", "tools/converse.py"):
        r = subprocess.run([sys.executable, str(PATCHES / "patch_ptt_instant.py"), str(pkg / rel)],
                           capture_output=True, text=True)
        assert r.returncode == 0 and "already patched" in r.stdout, r.stdout + r.stderr
        data = (pkg / rel).read_bytes()
        assert data == before[pkg / rel] and b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n")


def test_drift_fails_loudly(tmp_path):
    target = tmp_path / "control_channel.py"
    target.write_text("nothing to anchor on\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(PATCHES / "patch_ptt_instant.py"), str(target)],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "ANCHOR DRIFT" in r.stderr
