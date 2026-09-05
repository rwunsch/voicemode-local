"""Tests for patches/patch_listen_overrun.py (surgical converse.py patcher).

Sources come from VM812_SRC rather than a vendored fixture: converse.py is
4,620 lines / ~229KB in 8.12.0, and a blob that size goes stale silently. The
pristine copy is fetched into VM812_SRC by the audit tooling instead.

Run:
    VM812_SRC=<dir with pristine 8.12.0 converse.py> pytest tests/test_listen_overrun_patch.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHER = REPO / "patches" / "patch_listen_overrun.py"
HOLD_PATCHER = REPO / "patches" / "patch_converse_hold.py"


@pytest.fixture(scope="module")
def src_dir() -> Path:
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    p = Path(raw)
    if not (p / "converse.py").exists():
        pytest.fail(f"VM812_SRC={p} has no converse.py")
    return p


@pytest.fixture
def pristine_copy(tmp_path, src_dir) -> Path:
    target = tmp_path / "converse.py"
    shutil.copy(src_dir / "converse.py", target)
    return target


def run(patcher: Path, target: Path):
    return subprocess.run([sys.executable, str(patcher), str(target)],
                          capture_output=True, text=True)


def test_patch_applies_cleanly(pristine_copy):
    r = run(PATCHER, pristine_copy)
    assert r.returncode == 0, r.stderr
    out = pristine_copy.read_text()
    assert "voicemode-local listen overrun" in out
    assert "_hard_max_duration" in out


def test_patch_is_idempotent(pristine_copy):
    assert run(PATCHER, pristine_copy).returncode == 0
    r = run(PATCHER, pristine_copy)
    assert r.returncode == 0
    assert "already patched" in r.stdout
    # The marker appears once; _hard_max_duration legitimately appears
    # several times (one assignment + its uses in the loop condition).
    assert pristine_copy.read_text().count("voicemode-local listen overrun") == 1


def test_patch_preserves_upstream_stall_backstop(pristine_copy):
    """AUDIO_STALL_TIMEOUT is upstream's dead-stream guard and must survive."""
    assert run(PATCHER, pristine_copy).returncode == 0
    out = pristine_copy.read_text()
    assert "time.monotonic() - last_audio_time < AUDIO_STALL_TIMEOUT" in out


def test_patch_fails_loudly_on_drift(tmp_path):
    """The anchor is a drift detector — it must not silently no-op."""
    fake = tmp_path / "converse.py"
    fake.write_text("def record_audio_with_silence_detection():\n    pass\n")
    r = run(PATCHER, fake)
    assert r.returncode == 1
    assert "anchor" in r.stderr.lower()


def test_result_is_valid_python(pristine_copy):
    import ast
    assert run(PATCHER, pristine_copy).returncode == 0
    ast.parse(pristine_copy.read_text())


def test_composes_with_the_ptt_hold_patch_in_either_order(tmp_path, src_dir):
    """Both patch converse.py; neither may clobber the other."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    shutil.copy(src_dir / "converse.py", a)
    shutil.copy(src_dir / "converse.py", b)

    assert run(PATCHER, a).returncode == 0
    assert run(HOLD_PATCHER, a).returncode == 0

    assert run(HOLD_PATCHER, b).returncode == 0
    assert run(PATCHER, b).returncode == 0

    assert a.read_text() == b.read_text(), "patch order changes the result"
