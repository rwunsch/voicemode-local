"""Tests for patch_turns_playback_stop.py — downstream carry of upstream PR #555.

Upstream's own behavioural tests for the change live in the PR
(tests/test_turns_playback_cancellation.py); these check the patch mechanics.

Run:
    VM812_SRC=<dir with pristine 8.12.0 converse.py> pytest tests/test_turns_playback_stop_patch.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PATCH = Path(__file__).resolve().parent.parent / "patches" / "patch_turns_playback_stop.py"


@pytest.fixture
def target(tmp_path) -> Path:
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    t = tmp_path / "converse.py"
    shutil.copy(Path(raw) / "converse.py", t)
    return t


def run(t: Path):
    return subprocess.run([sys.executable, str(PATCH), str(t)], capture_output=True, text=True)


def test_applies_and_wires_the_stop_event(target):
    r = run(target)
    assert r.returncode == 0, r.stderr
    out = target.read_text()
    assert "def _play_samples_blocking(samples, sample_rate, stop_event" in out
    assert "player.play(samples, sample_rate, blocking=True)" not in out
    assert "stop_event=stop_playback," in out
    assert "except asyncio.CancelledError:\n                    stop_playback.set()\n                    raise" in out


def test_is_idempotent(target):
    assert run(target).returncode == 0
    first = target.read_text()
    r = run(target)
    assert r.returncode == 0 and "already patched" in r.stdout
    assert target.read_text() == first


def test_fails_loudly_when_upstream_changes(target):
    target.write_text(target.read_text().replace("blocking=True)", "blocking=True, x=1)"))
    r = run(target)
    assert r.returncode == 1
    assert "ANCHOR DRIFT" in r.stderr
