"""The transcript-saving git call is bounded (patches/patch_git_timeout.py).

Windows 2026-11-13 (n=1, reported by a peer session): config.get_project_path ran
`git rev-parse --show-toplevel` with no timeout and the MCP stdio pipe as stdin; it hung
176 s on the event loop and the conch stayed held. Run with:
VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_git_timeout_patch.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
SCRIPT = PATCHES / "patch_git_timeout.py"


@pytest.fixture
def config(tmp_path):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    shutil.copyfile(Path(raw) / "config.py", tmp_path / "config.py")
    return tmp_path / "config.py"


def _apply(target):
    return subprocess.run([sys.executable, str(SCRIPT), str(target)], capture_output=True, text=True)


def _get_project_path_ns(target):
    """Exec just get_project_path from the patched file, so config's import side effects stay out."""
    import ast
    import logging
    source = target.read_text()
    node = next(n for n in ast.parse(source).body if getattr(n, "name", "") == "get_project_path")
    ns = {"os": os, "subprocess": subprocess, "logging": logging, "logger": logging.getLogger("voicemode")}
    exec(compile(ast.get_source_segment(source, node), str(target), "exec"), ns)
    return ns


def test_applies_compiles_and_is_idempotent(config):
    r = _apply(config)
    assert r.returncode == 0, r.stderr
    text = config.read_text()
    compile(text, "config.py", "exec")
    assert "VML: GIT TIMEOUT" in text
    again = _apply(config)
    assert again.returncode == 0 and config.read_text() == text
    assert again.stdout == "" and again.stderr == ""


def test_the_git_call_has_stdin_closed_and_a_timeout(config, monkeypatch):
    assert _apply(config).returncode == 0
    ns = _get_project_path_ns(config)
    seen = {}

    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="/the/root\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ns["get_project_path"]() == "/the/root"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["timeout"] == 5
    assert seen["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_a_timeout_falls_back_to_cwd_and_does_not_raise(config, monkeypatch, caplog):
    assert _apply(config).returncode == 0
    ns = _get_project_path_ns(config)

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)
    with caplog.at_level("WARNING", logger="voicemode"):
        assert ns["get_project_path"]() == os.getcwd()
    assert any("git" in rec.getMessage() for rec in caplog.records)


def test_anchor_drift_fails_with_a_clear_message(config):
    config.write_text(config.read_text().replace("cwd=os.getcwd()\n        )", "cwd=os.getcwd() \n        )"))
    r = _apply(config)
    assert r.returncode != 0
    assert "anchor" in r.stderr and "not 1" in r.stderr


def test_an_older_version_is_noticed_not_overwritten(config):
    assert _apply(config).returncode == 0
    text = config.read_text().replace("timeout=5", "timeout=9")
    config.write_text(text)
    r = _apply(config)
    assert r.returncode == 0 and "older version" in r.stderr
    assert config.read_text() == text


def test_crlf_is_preserved(config):
    config.write_bytes(config.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert _apply(config).returncode == 0
    raw = config.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
