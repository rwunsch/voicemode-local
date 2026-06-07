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
