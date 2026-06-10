"""Tests for patches/patch_converse_queue.py (surgical converse.py patcher)."""
import subprocess
import sys
from glob import glob
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
PATCHER = REPO / "patches" / "patch_converse_queue.py"


def _installed_converse():
    hits = glob(str(REPO / ".venv" / "lib" / "python*" /
                    "site-packages" / "voice_mode" / "tools" / "converse.py"))
    hits += glob(str(REPO / ".venv" / "Lib" / "site-packages" /
                     "voice_mode" / "tools" / "converse.py"))
    return Path(hits[0]) if hits else None


@pytest.fixture
def converse_copy(tmp_path):
    src = _installed_converse()
    if src is None:
        pytest.skip("voice-mode not installed in .venv")
    dst = tmp_path / "converse.py"
    dst.write_text(src.read_text())
    return dst


def test_patch_applies_cleanly(converse_copy):
    r = subprocess.run([sys.executable, str(PATCHER), str(converse_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    text = converse_copy.read_text()
    assert "voice_queue" in text
    assert "ticket: Optional[str] = None" in text
    assert "end_burst" in text
    assert "queue_session.acquire" in text
    # Old arbitration gone
    assert "conch.try_acquire()" not in text
    assert "CONCH_ACQUIRE" not in text
    # Patched file must still be valid Python
    compile(text, str(converse_copy), "exec")


def test_patch_is_idempotent(converse_copy):
    subprocess.run([sys.executable, str(PATCHER), str(converse_copy)], check=True)
    r = subprocess.run([sys.executable, str(PATCHER), str(converse_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "already patched" in r.stdout.lower()


def test_patch_fails_loudly_on_drift(tmp_path):
    bogus = tmp_path / "converse.py"
    bogus.write_text("def converse(): pass\n")
    r = subprocess.run([sys.executable, str(PATCHER), str(bogus)],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "anchor" in (r.stdout + r.stderr).lower()


@pytest.fixture
def pristine_copy(tmp_path):
    """Unpatched upstream converse.py (vendored from the 8.7.1 wheel) — unlike
    the installed copy, this exercises the patcher's apply path, not its
    already-patched skip path."""
    src = REPO / "tests" / "fixtures" / "converse-8.7.1-pristine.py"
    dst = tmp_path / "converse.py"
    dst.write_text(src.read_text())
    return dst


def test_patch_wires_no_speech_timeout(pristine_copy):
    """Contention must yield via a no-speech timeout, never a hard listen cap
    (the 2026-06-11 truncation bug: capping listen_duration_max cut the user
    off mid-sentence at ~8s while still speaking)."""
    r = subprocess.run([sys.executable, str(PATCHER), str(pristine_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    text = pristine_copy.read_text()
    # recording function accepts the timeout
    assert "no_speech_timeout: Optional[float] = None" in text
    # all three recording call sites pass it through
    assert text.count(", _queue_no_speech_timeout\n") == 3
    # device-recovery retry inside the recording function preserves it
    assert ("record_audio_with_silence_detection(max_duration, "
            "disable_silence_detection, min_duration, vad_aggressiveness, "
            "no_speech_timeout)") in text
    # the WAITING_FOR_SPEECH state stops at the timeout
    assert "recording_duration >= no_speech_timeout" in text
    # queue wiring uses the new method; the hard cap is gone
    assert "effective_no_speech_timeout" in text
    assert "effective_listen_seconds" not in text
    compile(text, str(pristine_copy), "exec")
