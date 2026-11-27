"""patches/patch_audio_deadline.py against pristine voice-mode 8.12.0 (FELIX #2095).

The patched package is imported in a child process (its own HOME), so the real chimes,
status reply and converse wrapper run, with only the audio player replaced by a fake that
never calls back: upstream waits forever there, on the event loop.
"""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
MARKER = "voicemode-local audio deadline"


def _pkg_src() -> Path:
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    pkg = Path(raw).parent / "pkg" / "voice_mode"
    if not (pkg / "core.py").is_file():
        pytest.fail(f"no pristine package beside VM812_SRC: {pkg}")
    return pkg


@pytest.fixture(scope="module", params=["posix", "windows"])
def tree(request, tmp_path_factory):
    root = tmp_path_factory.mktemp(f"vm-{request.param}")
    shutil.copytree(_pkg_src(), root / "voice_mode")
    cmd = [sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(root / "voice_mode")]
    if request.param == "windows":
        cmd.append("--native-windows")
    out = subprocess.run(cmd, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    return root


def _run(root: Path, tmp_path: Path, body: str, env=None) -> subprocess.CompletedProcess:
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(body))
    e = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(root),
             VOICEMODE_SKIP_STARTUP_CHECKS="true")
    e.update(env or {})
    return subprocess.run([sys.executable, "-X", "utf8", str(script)],
                          capture_output=True, text=True, env=e, timeout=120, cwd=str(tmp_path))


def test_markers_and_idempotence(tree):
    pkg = tree / "voice_mode"
    assert (pkg / "core.py").read_text().count(MARKER) == 5
    assert (pkg / "tools" / "converse.py").read_text().count(MARKER) == 3
    assert (pkg / "control_socket.py").read_text().count(MARKER) == 2
    before = {p: p.read_bytes() for p in (pkg / "core.py", pkg / "tools" / "converse.py", pkg / "control_socket.py")}
    rc = subprocess.run([sys.executable, str(PATCHES / "patch_audio_deadline.py"), *map(str, before)]).returncode
    assert rc == 0
    assert all(p.read_bytes() == b for p, b in before.items())
    assert "blocking=True" not in (pkg / "core.py").read_text()


def test_changed_voice_mode_is_refused(tmp_path):
    core = tmp_path / "core.py"
    core.write_text("print('not voice-mode')\n")
    out = subprocess.run([sys.executable, str(PATCHES / "patch_audio_deadline.py"), str(core)],
                         capture_output=True, text=True)
    assert out.returncode == 1 and "voice-mode has changed" in out.stderr


def test_a_chime_on_a_dead_device_no_longer_freezes_the_loop(tree, tmp_path):
    out = _run(tree, tmp_path, """
        import asyncio, threading, time
        import voice_mode.core as core
        from voice_mode import audio_deadline

        class DeadPlayer:
            def __init__(self):
                self.playback_complete = threading.Event()
            def play(self, samples, rate, blocking=False):
                if blocking:
                    self.playback_complete.wait()   # upstream: forever
            def wait(self, timeout=None):
                self.playback_complete.wait(timeout)

        core.NonBlockingAudioPlayer = DeadPlayer
        ticks = []

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.05)

        async def go():
            t = asyncio.ensure_future(ticker())
            t0 = time.monotonic()
            ok = await core.play_chime_start()
            t.cancel()
            return ok, time.monotonic() - t0

        ok, took = asyncio.run(go())
        print("RESULT", ok, round(took, 1), len(ticks) > 5, bool(audio_deadline.wedged))
        """, env={"VOICEMODE_AUDIO_DEADLINE_S": "0.5"})
    assert "RESULT False" in out.stdout and " True True" in out.stdout, out.stdout + out.stderr
    took = float(out.stdout.split("RESULT False ")[1].split()[0])
    assert took < 5


def test_the_status_reply_carries_the_loop_lag(tree, tmp_path):
    out = _run(tree, tmp_path, """
        import inspect
        import voice_mode.control_socket as cs
        src = inspect.getsource(cs)
        print("HAS", '"loop_lag_s": _vml_loop_lag()' in src, cs._vml_loop_lag())
        """)
    assert "HAS True None" in out.stdout, out.stdout + out.stderr


def test_a_wedged_process_answers_converse_with_what_to_do(tree, tmp_path):
    out = _run(tree, tmp_path, """
        import asyncio
        from voice_mode.tools import converse as cv
        from voice_mode import audio_deadline
        exits = []
        audio_deadline._exit_now = lambda code=75: exits.append(code)
        audio_deadline.EXIT_DELAY_S = 0.05

        async def never(*a, **k):
            raise AssertionError("the inner converse must not run")

        cv._vml_converse_core_before_deadline = never
        audio_deadline.mark_wedged("playback did not finish within 10 s")

        async def go():
            r = await cv._converse_core("hello")
            await asyncio.sleep(0.5)
            return r

        print("RESULT", asyncio.run(go()))
        print("EXITS", exits)
        """)
    assert "restart the chat" in out.stdout and "EXITS [75]" in out.stdout, out.stdout + out.stderr


def test_a_missed_deadline_inside_converse_becomes_the_message(tree, tmp_path):
    out = _run(tree, tmp_path, """
        import asyncio
        from voice_mode.tools import converse as cv
        from voice_mode import audio_deadline
        audio_deadline._exit_now = lambda code=75: None

        async def hangs(*a, **k):
            await audio_deadline.call(lambda: __import__("time").sleep(3600), 0.3,
                                      "refreshing the audio devices")

        cv._vml_converse_core_before_deadline = hangs
        print("RESULT", asyncio.run(cv._converse_core("hello")))
        """)
    assert "refreshing the audio devices did not return within 0.3 s" in out.stdout, out.stdout + out.stderr
