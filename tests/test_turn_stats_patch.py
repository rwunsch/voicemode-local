"""patches/patch_turn_stats.py against pristine voice-mode 8.12.0 (FELIX #2107).

The patched recording loop runs in a child process (its own HOME) with a fake microphone
(sounddevice.InputStream feeds silent 30 ms chunks from a thread) and a scripted webrtcvad,
so each exit of the loop is driven for real and its end_reason checked.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
MARKER = "voicemode-local turn stats"


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


PROBE = r'''
import json, sys, threading, time
import numpy as np
import sounddevice as sd
import webrtcvad

SCRIPT = sys.argv[1]          # one char per frame: S speech, . silence; the rest repeats the last
ACTION = sys.argv[2]          # none | done | stop
MAX = float(sys.argv[3])

class FakeVad:
    def __init__(self, *a): self.i = 0
    def is_speech(self, b, rate):
        ch = SCRIPT[min(self.i, len(SCRIPT) - 1)]; self.i += 1
        return ch == "S"

class FakeStream:
    def __init__(self, samplerate, channels, dtype, callback, blocksize, **kw):
        self.cb, self.n, self.on = callback, blocksize, False
    def __enter__(self):
        self.on = True
        def run():
            while self.on:
                self.cb(np.full((self.n, 1), 40, dtype=np.int16), self.n, None, None)
                time.sleep(0.001)
        threading.Thread(target=run, daemon=True).start()
        return self
    def __exit__(self, *a): self.on = False

webrtcvad.Vad = FakeVad
sd.InputStream = FakeStream
from voice_mode.tools import converse
from voice_mode import turn_stats
from voice_mode.control_channel import get_control_state
since = []
if ACTION == "since":
    from voice_mode import control_socket
    def sample():   # poll: the recording starts only after the device refresh (~0.7 s, varies)
        while True:
            v = control_socket._vml_listening_since()
            if v is not None:
                since.append(v)
                return
            time.sleep(0.01)
    threading.Thread(target=sample, daemon=True).start()
elif ACTION != "none":
    def later():
        time.sleep(0.3)
        st = get_control_state()
        (st.request_skip_forward if ACTION == "done" else st.request_stop)()
    threading.Thread(target=later, daemon=True).start()
t0 = time.time()
converse.record_audio_with_silence_detection(MAX)
out = converse._vml_turn_stats_fields()   # the helper the RECORDING_END event calls
if ACTION == "since":
    from voice_mode import control_socket
    out["_since_during"] = since[0] if since else None
    out["_since_after"] = control_socket._vml_listening_since()
    out["_t0"] = t0
print(json.dumps(out))
'''


def _record(root, tmp_path, script, action="none", max_s=5.0, env=None):
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    # No inherited VOICEMODE_* settings: in the full suite another test has loaded the real
    # ~/.voicemode/voicemode.env into os.environ (here VOICEMODE_SILENCE_THRESHOLD_MS=2000).
    base = {k: v for k, v in os.environ.items() if not k.startswith("VOICEMODE_")}
    e = dict(base, HOME=str(tmp_path), PYTHONPATH=str(root), VOICEMODE_SKIP_STARTUP_CHECKS="true",
             VOICEMODE_BASE_DIR=str(tmp_path / ".voicemode"), VOICEMODE_LISTEN_OVERRUN="0")
    e.update(env or {})
    out = subprocess.run([sys.executable, "-X", "utf8", str(probe), script, action, str(max_s)],
                         capture_output=True, text=True, env=e, timeout=120, cwd=str(tmp_path))
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_markers_and_idempotence(tree):
    conv = tree / "voice_mode" / "tools" / "converse.py"
    assert conv.read_text(encoding="utf-8").count(MARKER) == 17
    sock = tree / "voice_mode" / "control_socket.py"
    assert sock.read_text(encoding="utf-8").count(MARKER) == 2
    src = conv.read_text(encoding="utf-8")
    event = src[src.index("event_logger.RECORDING_END, {"):][:300]
    assert "**_vml_turn_stats_fields()" in event      # the fields really go into the event
    assert (tree / "voice_mode" / "turn_stats.py").is_file()
    before = conv.read_bytes()
    assert subprocess.run([sys.executable, str(PATCHES / "patch_turn_stats.py"), str(conv)]).returncode == 0
    assert conv.read_bytes() == before


def test_a_pause_ends_the_turn_as_silence(tree, tmp_path):
    f = _record(tree, tmp_path, "SSSSSSSSSSSSSSSSSSSSSSSSS.")    # 0.75 s speech, then silence
    assert f["end_reason"] == "silence"
    assert f["longest_pause_ms"] >= 1000 and f["silence_needed_ms"] == 1000
    assert f["longest_inner_pause_ms"] == 0          # no pause inside the speech
    assert f["frames"] > 25 and 0 < f["speech_share"] < 1
    assert f["quiet_dbfs"] is not None and f["quiet_dbfs"] < -40


def test_no_speech_runs_to_the_window_end(tree, tmp_path):
    f = _record(tree, tmp_path, ".", max_s=0.6)
    assert f["end_reason"] == "no_speech" and f["speech_share"] == 0.0


def test_speech_that_never_pauses_hits_the_cap(tree, tmp_path):
    f = _record(tree, tmp_path, "S", max_s=0.6)
    assert f["end_reason"] == "cap" and f["pause_resets"] == 0 and f["speech_share"] == 1.0


def test_done_while_listening_is_named(tree, tmp_path):
    f = _record(tree, tmp_path, "S", action="done", max_s=10, env={"VOICEMODE_CONTROL_ENABLED": "true"})
    assert f["end_reason"] == "done"


def test_stop_while_listening_is_named(tree, tmp_path):
    f = _record(tree, tmp_path, "S", action="stop", max_s=10, env={"VOICEMODE_CONTROL_ENABLED": "true"})
    assert f["end_reason"] == "stop"


def test_silence_detection_off_is_no_vad(tree, tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent('''
        import json
        from voice_mode.tools import converse
        from voice_mode import turn_stats
        converse.record_audio = lambda d: __import__("numpy").zeros(10, dtype="int16")
        converse.record_audio_with_silence_detection(1.0, disable_silence_detection=True)
        print(json.dumps(turn_stats.event_fields()))
    '''))
    e = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=str(tree), VOICEMODE_SKIP_STARTUP_CHECKS="true",
             VOICEMODE_BASE_DIR=str(tmp_path / ".voicemode"))
    out = subprocess.run([sys.executable, "-X", "utf8", str(probe)], capture_output=True, text=True,
                         env=e, timeout=120, cwd=str(tmp_path))
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1])["end_reason"] == "no_vad"


# Review of a51c406 (F3): the pill's note needs the start of every recording, not upstream's
# barge-only listen_started_at. The status reply's listening_since is set while recording.
def test_listening_since_is_set_while_recording(tree, tmp_path):
    f = _record(tree, tmp_path, "S", action="since", max_s=1.0)
    assert f["_since_during"] is not None and abs(f["_since_during"] - f["_t0"]) < 2
    assert f["_since_after"] is None
