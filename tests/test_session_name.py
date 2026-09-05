"""Tests for patch_session_name.py — distinguishable conch names per session.

Upstream hardcodes agent_name="converse", so concurrent sessions are
indistinguishable in `voicemode conch status`. This patch resolves a real label.

Run:
    VM812_SRC=<dir with pristine 8.12.0 converse.py> pytest tests/test_session_name.py
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"


@pytest.fixture(scope="module")
def src_dir() -> Path:
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    p = Path(raw)
    if not (p / "converse.py").exists():
        pytest.fail(f"VM812_SRC={p} has no converse.py")
    return p


@pytest.fixture(scope="module")
def patched(tmp_path_factory, src_dir) -> str:
    work = tmp_path_factory.mktemp("sessname")
    target = work / "converse.py"
    shutil.copy(src_dir / "converse.py", target)
    r = subprocess.run(
        [sys.executable, str(PATCHES / "patch_session_name.py"), str(target)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"patch failed: {r.stderr}"
    return target.read_text()


@pytest.fixture(scope="module")
def resolver(patched):
    """Extract and exec just the helper, so no voice_mode import is needed."""
    m = re.search(r"\ndef _vml_session_name\(\) -> str:.*?\n    return \"converse\"\n",
                  patched, re.S)
    assert m, "helper not found in patched source"
    ns = {}
    exec(m.group(0), ns)
    return ns["_vml_session_name"]


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


# --- the patch itself -------------------------------------------------------

def test_hardcoded_name_is_replaced(patched):
    """The Conch construction line must no longer hardcode the name.

    Matched with its indentation so the patch's own docstring -- which quotes
    the old line as prose -- doesn't produce a false failure.
    """
    assert '\n        agent_name="converse",\n' not in patched
    assert "agent_name=_vml_session_name()" in patched


def test_helper_is_module_level(patched):
    """A nested def would not be in scope at the Conch construction site."""
    assert re.search(r"^def _vml_session_name", patched, re.M), \
        "helper is indented — it must be module-level"


def test_helper_defined_before_use(patched):
    assert patched.index("def _vml_session_name") < patched.index("agent_name=_vml_session_name")


# --- resolution order -------------------------------------------------------

def test_env_override_wins(resolver, monkeypatch, clean_env):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "queue-naming")
    assert resolver() == "queue-naming"


def test_env_override_is_stripped(resolver, monkeypatch, clean_env):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "  windows-port \n")
    assert resolver() == "windows-port"


def test_blank_env_falls_through(resolver, monkeypatch, clean_env, tmp_path):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "   ")
    monkeypatch.chdir(tmp_path)
    assert resolver() == tmp_path.name


def test_session_names_file_is_read(resolver, monkeypatch, clean_env, tmp_path):
    home = tmp_path / "home"
    d = home / ".voicemode" / "session_names"
    d.mkdir(parents=True)
    (d / "SID123.txt").write_text("  upstream-realign \n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "SID123")
    assert resolver() == "upstream-realign"


def test_missing_label_file_falls_back_to_cwd(resolver, monkeypatch, clean_env, tmp_path):
    home = tmp_path / "home"
    (home / ".voicemode" / "session_names").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "NOSUCH")
    work = tmp_path / "my-repo"
    work.mkdir()
    monkeypatch.chdir(work)
    assert resolver() == "my-repo"


def test_label_is_length_capped(resolver, monkeypatch, clean_env):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "x" * 500)
    assert len(resolver()) == 64


def test_never_raises_on_an_unreadable_home(resolver, monkeypatch, clean_env, tmp_path):
    """A label is a convenience — it must never cost the caller a turn."""
    # HOME points at a regular FILE, so the session_names path is not a dir.
    broken = tmp_path / "not-a-dir"
    broken.write_text("")
    monkeypatch.setenv("HOME", str(broken))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "SID")
    assert isinstance(resolver(), str)


def test_never_raises_when_cwd_is_gone(resolver, monkeypatch, clean_env, tmp_path):
    """os.getcwd() raises if the working directory was deleted underneath us."""
    gone = tmp_path / "vanishing"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    assert resolver() == "converse"  # final fallback, no exception
