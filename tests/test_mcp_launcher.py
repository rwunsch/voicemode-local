"""Exercise launcher cleanup without starting audio services."""

import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def launcher(tmp_path):
    script = tmp_path / "voicemode-mcp"
    shutil.copy(REPO / "voicemode-mcp", script)
    ensure = tmp_path / "voicemode-switch"
    ensure.write_text("#!/usr/bin/env bash\nexit 0\n")
    ensure.chmod(0o755)
    server = tmp_path / ".venv" / "bin" / "voice-mode"
    server.parent.mkdir(parents=True)
    server.write_text(
        f"#!{sys.executable}\n"
        "import time\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    server.chmod(0o755)
    processes = []

    def start(session_id=None):
        env = dict(os.environ, HOME=str(tmp_path))
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if session_id is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session_id
        process = subprocess.Popen(
            [str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(process)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "launcher did not become ready"
        assert process.stdout.readline() == "ready\n"
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_unidentified_sessions_ignore_legacy_parent_pidfile(launcher, tmp_path):
    first = launcher()
    run_dir = tmp_path / ".voicemode" / "run"
    (run_dir / f"voicemode-ppid-{os.getpid()}.pid").write_text(str(first.pid))
    second = launcher()
    assert first.poll() is None
    second.terminate()
    second.wait(timeout=5)
    assert first.poll() is None


def test_distinct_sessions_sharing_parent_survive(launcher):
    first = launcher("session-one")
    second = launcher("session-two")
    assert first.poll() is None
    assert second.poll() is None


def test_same_explicit_session_reaps_previous_server(launcher):
    first = launcher("same-session")
    second = launcher("same-session")
    assert first.wait(timeout=5) != 0
    assert second.poll() is None


def _wsl_launcher_env(tmp_path, pill):
    """A WSL-looking environment: WSL_DISTRO_NAME set, a powershell.exe on PATH, and the
    pill setting in voicemode.env. The fake voicemode-switch records its arguments."""
    (tmp_path / ".voicemode").mkdir(exist_ok=True)
    (tmp_path / ".voicemode" / "voicemode.env").write_text(f"VOICEMODE_PILL={pill}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ps = bin_dir / "powershell.exe"
    ps.write_text("#!/usr/bin/env bash\nexit 0\n")
    ps.chmod(0o755)
    calls = tmp_path / "switch-calls.txt"
    (tmp_path / "voicemode-switch").write_text(
        f'#!/usr/bin/env bash\necho "$*" >> "{calls}"\nexit 0\n')
    return {"WSL_DISTRO_NAME": "Ubuntu", "PATH": f"{bin_dir}:{os.environ['PATH']}"}, calls


def _start_with(launcher_dir, extra):
    import time
    env = dict(os.environ, HOME=str(launcher_dir), **extra)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    p = subprocess.Popen([str(launcher_dir / "voicemode-mcp")], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    assert p.stdout.readline() == "ready\n"
    time.sleep(1.0)  # the pill start runs in the background
    p.terminate()
    p.wait(timeout=5)
    p.stdout.close(); p.stderr.close()


def test_wsl_session_start_leaves_the_pill_to_the_voice_server(launcher, tmp_path):
    """Robert 2026-11-10: the voice server opens the pill itself (pill_autostart) at a
    session's first voice turn, on every OS. The launcher no longer starts it."""
    launcher  # builds the launcher and its fakes in tmp_path
    extra, calls = _wsl_launcher_env(tmp_path, "true")
    _start_with(tmp_path, extra)
    assert not calls.exists() or "pill" not in calls.read_text()
    assert "ensure_pill_window" not in (REPO / "voicemode-mcp").read_text()


def test_wsl_session_start_leaves_the_pill_alone_when_it_is_off(launcher, tmp_path):
    launcher
    extra, calls = _wsl_launcher_env(tmp_path, "false")
    _start_with(tmp_path, extra)
    assert not calls.exists() or "pill" not in calls.read_text()
