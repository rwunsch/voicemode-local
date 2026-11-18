"""patches/patch_vad_override.py: record_audio_with_silence_detection resolves VAD
aggressiveness and the silence threshold at each recording through
voice_mode.voice_sensitivity.resolve (this call's argument > sensitivity.json > config), so
the voice_sensitivity tool's change applies at the next listen without a restart. With no
override file the values are config's, as before.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
PATCHER = PATCHES / "patch_vad_override.py"
MARKER = "voicemode-local vad override"

# The shape of upstream's function: the anchor lines are upstream 8.12.0's, verbatim.
CONVERSE = '''\
from typing import Optional
import threading

VAD_AGGRESSIVENESS = 2
SILENCE_THRESHOLD_MS = 2000
LOG = []


def record_audio_with_silence_detection(max_duration: float, disable_silence_detection: bool = False, min_duration: float = 0.0, vad_aggressiveness: Optional[int] = None, stop_event: Optional[threading.Event] = None):
    """Record audio."""
    try:
        # Initialize VAD with provided aggressiveness or default
        effective_vad_aggressiveness = vad_aggressiveness if vad_aggressiveness is not None else VAD_AGGRESSIVENESS
        LOG.append(f"Silence threshold: {SILENCE_THRESHOLD_MS}ms")
        silence_duration_ms = 0
        recording_duration = 1.0
        effective_min_duration = 0.0
        while True:
            silence_duration_ms += 30
            if recording_duration >= effective_min_duration and silence_duration_ms >= SILENCE_THRESHOLD_MS:
                break
        return effective_vad_aggressiveness, silence_duration_ms
    except ValueError:
        raise


def unrelated():
    return SILENCE_THRESHOLD_MS
'''


def _run(target):
    return subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True,
                          text=True, env=dict(os.environ, PYTHONUTF8="1"))


def _load(path):
    spec = importlib.util.spec_from_file_location("vad_fixture_" + uuid.uuid4().hex[:8], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sens(monkeypatch, tmp_path):
    """The real voice_sensitivity module as voice_mode.voice_sensitivity, under a fake HOME."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("VOICEMODE_BASE_DIR", raising=False)
    spec = importlib.util.spec_from_file_location("voice_mode.voice_sensitivity",
                                                  PATCHES / "voice_sensitivity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pkg = type(sys)("voice_mode")
    pkg.__path__ = []
    pkg.voice_sensitivity = mod
    monkeypatch.setitem(sys.modules, "voice_mode", pkg)
    monkeypatch.setitem(sys.modules, "voice_mode.voice_sensitivity", mod)
    return mod


@pytest.fixture()
def patched(tmp_path):
    target = tmp_path / "converse.py"
    target.write_text(CONVERSE, encoding="utf-8")
    r = _run(target)
    assert r.returncode == 0, r.stdout + r.stderr
    return target


def test_no_override_is_config_as_before(patched, sens):
    m = _load(patched)
    assert m.record_audio_with_silence_detection(10) == (2, 2010)
    assert m.LOG == ["Silence threshold: 2000ms"]
    original = _load_plain(patched.parent)
    assert m.record_audio_with_silence_detection(10) == original.record_audio_with_silence_detection(10)


def test_override_applies_at_the_next_recording(patched, sens):
    m = _load(patched)
    assert m.record_audio_with_silence_detection(10) == (2, 2010)
    sens.write_override(3, 1200, "earlier")
    assert m.record_audio_with_silence_detection(10) == (3, 1200)
    assert m.LOG[-1] == "Silence threshold: 1200ms"
    sens.clear_override()
    assert m.record_audio_with_silence_detection(10) == (2, 2010)


def test_explicit_argument_beats_the_override(patched, sens):
    m = _load(patched)
    sens.write_override(3, 800, "noisy")
    assert m.record_audio_with_silence_detection(10, vad_aggressiveness=0) == (0, 810)   # 30 ms steps


def test_only_the_recording_function_is_changed(patched, sens):
    m = _load(patched)
    sens.write_override(3, 800, None)
    assert m.unrelated() == 2000
    assert m.SILENCE_THRESHOLD_MS == 2000 and m.VAD_AGGRESSIVENESS == 2


def test_missing_module_falls_back_to_config(patched, monkeypatch):
    monkeypatch.setitem(sys.modules, "voice_mode", type(sys)("voice_mode"))
    monkeypatch.setitem(sys.modules, "voice_mode.voice_sensitivity", None)
    m = _load(patched)
    assert m.record_audio_with_silence_detection(10) == (2, 2010)
    assert m.record_audio_with_silence_detection(10, vad_aggressiveness=1) == (1, 2010)


def test_a_corrupt_override_file_falls_back_to_config(patched, sens):
    path = sens.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert _load(patched).record_audio_with_silence_detection(10) == (2, 2010)


def _load_plain(folder):
    plain = folder / "plain.py"
    plain.write_text(CONVERSE, encoding="utf-8")
    return _load(plain)


def test_idempotent(patched):
    once = patched.read_bytes()
    r = _run(patched)
    assert r.returncode == 0 and "already patched (vad override)" in r.stdout
    assert patched.read_bytes() == once
    assert once.decode("utf-8").count(MARKER) == 1


def test_keeps_crlf(tmp_path):
    target = tmp_path / "converse.py"
    target.write_bytes(CONVERSE.replace("\n", "\r\n").encode("utf-8"))
    assert _run(target).returncode == 0
    data = target.read_bytes()
    assert MARKER.encode() in data and b"\n" not in data.replace(b"\r\n", b"")


@pytest.mark.parametrize("change", [
    ("        effective_vad_aggressiveness = vad_aggressiveness if vad_aggressiveness is not None else VAD_AGGRESSIVENESS\n", ""),
    ("silence_duration_ms >= SILENCE_THRESHOLD_MS", "silence_duration_ms >= LIMIT"),
    ("def record_audio_with_silence_detection(", "def record_audio(") ,
])
def test_drift_fails_loudly_and_writes_nothing(tmp_path, change):
    target = tmp_path / "converse.py"
    target.write_text(CONVERSE.replace(*change), encoding="utf-8")
    before = target.read_bytes()
    r = _run(target)
    assert r.returncode == 1 and "ANCHOR DRIFT" in r.stderr and "(vad override)" in r.stderr
    assert target.read_bytes() == before


WRAPPED = CONVERSE.replace(
    "def record_audio_with_silence_detection(",
    "def record_audio_with_silence_detection(max_duration, disable_silence_detection=False, "
    "min_duration=0.0, vad_aggressiveness=None, stop_event=None):\n"
    "    return record_audio_with_silence_detection_inner(max_duration, disable_silence_detection, "
    "min_duration, vad_aggressiveness, stop_event)\n\n\n"
    "def record_audio_with_silence_detection_inner(")


def test_pill_wrapped_function_is_patched_in_the_inner_body(tmp_path, sens):
    """patch_voice_pill renames upstream's function to ..._inner behind a wrapper; the
    recording loop, and so the anchors, are in the inner one."""
    target = tmp_path / "converse.py"
    target.write_text(WRAPPED, encoding="utf-8")
    r = _run(target)
    assert r.returncode == 0, r.stdout + r.stderr
    m = _load(target)
    sens.write_override(3, 1200, None)
    assert m.record_audio_with_silence_detection(10) == (3, 1200)
    assert m.record_audio_with_silence_detection(10, vad_aggressiveness=1) == (1, 1200)


def test_no_provider_key_text():
    src = PATCHER.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in src and "CARTESIA_API_KEY" not in src


# -- the real thing: pristine 8.12.0 and the whole manifest ---------------------------

def _src():
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC is not set")
    return Path(raw)


def _function(text):
    inner = "def record_audio_with_silence_detection_inner("
    start = text.index(inner if inner in text else "def record_audio_with_silence_detection(")
    return text[start:text.index("\ndef ", start + 1)]


def _check(text):
    body = _function(text)
    assert MARKER in body
    assert "from voice_mode.voice_sensitivity import resolve" in body
    # Every use of the threshold inside the function reads the resolved value; the constant
    # is read only as the config default handed to resolve (and in the fallback).
    assert body.count("SILENCE_THRESHOLD_MS") == 2
    assert "silence_duration_ms >= _vml_silence_threshold_ms" in body
    compile(text, "converse.py", "exec")


def test_applies_to_pristine_8_12_0_and_is_idempotent(tmp_path):
    target = tmp_path / "converse.py"
    shutil.copy(_src() / "converse.py", target)
    pristine = target.read_text(encoding="utf-8")
    r = _run(target)
    assert r.returncode == 0, r.stdout + r.stderr
    text = target.read_text(encoding="utf-8")
    _check(text)
    # Outside the function nothing changed.
    outside = lambda t: t.replace(_function(t), "")
    assert outside(text) == outside(pristine)
    once = target.read_bytes()
    assert _run(target).returncode == 0 and target.read_bytes() == once


def _manifest(name):
    return json.loads((PATCHES / name).read_text(encoding="utf-8"))["steps"]


@pytest.mark.parametrize("name", ["manifest.json", "manifest.windows.json"])
def test_both_manifests_carry_the_module_and_the_patch(name):
    steps = _manifest(name)
    names = [s.get("patch") or s.get("copy") for s in steps]
    copy = next(s for s in steps if s.get("copy") == "voice_sensitivity.py")
    assert copy["to"] == "voice_sensitivity.py"
    patch = next(s for s in steps if s.get("patch") == "patch_vad_override.py")
    assert patch["targets"] == ["tools/converse.py"]
    # The module goes in before the patch makes converse import it, and the patch comes
    # after every other converse patch that edits the recording loop.
    assert names.index("voice_sensitivity.py") < names.index("patch_vad_override.py")
    for earlier in ("patch_listen_overrun.py", "patch_converse_hold.py", "patch_voice_barge.py",
                    "patch_voice_pill.py", "patch_ptt_instant.py"):
        assert names.index(earlier) < names.index("patch_vad_override.py"), earlier


@pytest.mark.parametrize("native", [False, True], ids=["default", "windows"])
def test_whole_manifest_applies_and_resolves_in_the_loop(tmp_path, native):
    src = _src()
    pkg = tmp_path / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    sources = {"control_channel.py": "control_channel.py", "control_socket.py": "control_socket.py",
               "core.py": "core.py", "simple_failover.py": "simple_failover.py",
               "streaming.py": "streaming.py", "server.py": "server.py",
               "tools/converse.py": "converse.py", "conch_notify.py": "conch_notify.py",
               "conch_queue.py": "conch_queue.py", "conch.py": "conch.py", "config.py": "config.py",
               "tools/configuration_management.py": "configuration_management.py"}
    for rel, name in sources.items():
        shutil.copy(src / name, pkg / rel)
    args = [sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(pkg)]
    if native:
        args.append("--native-windows")
    for _ in range(2):   # the second pass changes nothing
        r = subprocess.run(args, capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
        assert r.returncode == 0, r.stdout + r.stderr
    text = (pkg / "tools/converse.py").read_text(encoding="utf-8")
    _check(text)
    assert text.count(MARKER) == 1
    assert (pkg / "voice_sensitivity.py").read_bytes() == (PATCHES / "voice_sensitivity.py").read_bytes()
    # The hold patch's silence check still reads the resolved value.
    assert "and silence_duration_ms >= _vml_silence_threshold_ms):" in text
