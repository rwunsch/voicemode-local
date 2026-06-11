"""Tests for patches/patch_listen_overrun.py (surgical converse.py patcher)."""
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
PATCHER = REPO / "patches" / "patch_listen_overrun.py"
QUEUE_PATCHER = REPO / "patches" / "patch_converse_queue.py"


@pytest.fixture
def pristine_copy(tmp_path):
    """Unpatched upstream converse.py (vendored from the 8.7.1 wheel)."""
    src = REPO / "tests" / "fixtures" / "converse-8.7.1-pristine.py"
    dst = tmp_path / "converse.py"
    dst.write_text(src.read_text())
    return dst


def _run(patcher, target):
    return subprocess.run([sys.executable, str(patcher), str(target)],
                          capture_output=True, text=True)


def test_patch_applies_cleanly(pristine_copy):
    r = _run(PATCHER, pristine_copy)
    assert r.returncode == 0, r.stdout + r.stderr
    text = pristine_copy.read_text()
    # the hard cap is replaced by the speech-aware condition
    assert "while recording_duration < max_duration and not stop_recording:" \
        not in text
    assert "_hard_max_duration if speech_detected else max_duration" in text
    assert "VOICEMODE_LISTEN_OVERRUN" in text
    compile(text, str(pristine_copy), "exec")


def test_patch_is_idempotent(pristine_copy):
    assert _run(PATCHER, pristine_copy).returncode == 0
    r = _run(PATCHER, pristine_copy)
    assert r.returncode == 0
    assert "already patched" in r.stdout.lower()


def test_patch_fails_loudly_on_drift(tmp_path):
    bogus = tmp_path / "converse.py"
    bogus.write_text("def converse(): pass\n")
    r = _run(PATCHER, bogus)
    assert r.returncode != 0
    assert "anchor" in (r.stdout + r.stderr).lower()


def test_patch_composes_with_queue_patch(pristine_copy):
    """apply.sh runs the queue patcher first — the overrun patcher must still
    find its anchor afterwards (and vice versa)."""
    assert _run(QUEUE_PATCHER, pristine_copy).returncode == 0
    r = _run(PATCHER, pristine_copy)
    assert r.returncode == 0, r.stdout + r.stderr
    text = pristine_copy.read_text()
    assert "_hard_max_duration if speech_detected else max_duration" in text
    assert "_queue_no_speech_timeout" in text  # queue patch intact
    compile(text, str(pristine_copy), "exec")
