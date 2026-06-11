"""Tests for patches/patch_converse_cancel.py (re-raise client cancellations).

Upstream 8.7.1 swallows asyncio.CancelledError in converse() and returns a
result; under fastmcp 3.x / mcp>=1.26 the SDK has already responded to the
cancelled request, so the extra return double-responds and crashes the whole
MCP server ("assert not self._completed" → next converse gets MCP -32000
Connection closed). The patch keeps the TOOL_CANCELLED logging and re-raises.
"""
import subprocess
import sys
from glob import glob
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
PATCHER = REPO / "patches" / "patch_converse_cancel.py"


def _installed_converse():
    hits = glob(str(REPO / ".venv" / "lib" / "python*" /
                    "site-packages" / "voice_mode" / "tools" / "converse.py"))
    hits += glob(str(REPO / ".venv" / "Lib" / "site-packages" /
                     "voice_mode" / "tools" / "converse.py"))
    return Path(hits[0]) if hits else None


@pytest.fixture
def pristine_copy(tmp_path):
    """Unpatched upstream converse.py (vendored from the 8.7.1 wheel)."""
    src = REPO / "tests" / "fixtures" / "converse-8.7.1-pristine.py"
    dst = tmp_path / "converse.py"
    dst.write_text(src.read_text())
    return dst


def test_patch_applies_cleanly(pristine_copy):
    r = subprocess.run([sys.executable, str(PATCHER), str(pristine_copy)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    text = pristine_copy.read_text()
    # The swallow-and-return is gone; the cancellation propagates.
    assert 'result = "Cancelled by user."' not in text
    assert "RE-RAISE on client cancel (voicemode-local)" in text
    # Cancellation diagnostics survive the rewrite.
    assert '"TOOL_CANCELLED"' in text
    # The except block ends in a bare raise (finally still runs after it).
    assert "        success = False\n        raise\n" in text
    compile(text, str(pristine_copy), "exec")


def test_patch_composes_with_queue_patch(pristine_copy):
    """Both converse patchers must apply to the same file in apply.sh order."""
    queue_patcher = REPO / "patches" / "patch_converse_queue.py"
    for patcher in (queue_patcher, PATCHER):
        r = subprocess.run([sys.executable, str(patcher), str(pristine_copy)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{patcher.name}: {r.stdout}{r.stderr}"
    text = pristine_copy.read_text()
    assert "queue_session.acquire" in text
    assert "RE-RAISE on client cancel (voicemode-local)" in text
    compile(text, str(pristine_copy), "exec")


def test_patch_is_idempotent(pristine_copy):
    subprocess.run([sys.executable, str(PATCHER), str(pristine_copy)], check=True)
    r = subprocess.run([sys.executable, str(PATCHER), str(pristine_copy)],
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


def test_installed_copy_is_patched():
    """Guard: the venv copy actually carries the fix (apply.sh was run)."""
    installed = _installed_converse()
    if installed is None:
        pytest.skip("voice-mode not installed in .venv")
    text = installed.read_text()
    if "RE-RAISE on client cancel (voicemode-local)" not in text:
        pytest.fail("installed converse.py lacks the cancel re-raise patch — "
                    "run ./patches/apply.sh")
