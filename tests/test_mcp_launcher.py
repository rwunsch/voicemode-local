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


def test_same_session_from_a_different_client_does_not_reap(launcher, tmp_path):
    """2026-11-26: `copilot -p` run from a Claude Code Bash tool inherits
    CLAUDE_CODE_SESSION_ID; its voicemode launch must not kill Claude Code's live
    server for that session. A different client = a parent with another name."""
    import signal
    first = launcher("shared-session")
    # A symlink, not a copy: macOS SIGKILLs a copied /bin/bash, and `ps -o comm=`
    # reports the name the process was started under, so the parent looks like
    # a different client.
    other_client = tmp_path / "other-client"
    os.symlink(shutil.which("bash"), other_client)
    env = dict(os.environ, HOME=str(tmp_path), CLAUDE_CODE_SESSION_ID="shared-session")
    # "; true" keeps bash alive as the launcher's parent (no exec of the last command).
    second = subprocess.Popen(
        [str(other_client), "-c", f'"{tmp_path / "voicemode-mcp"}"; true'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(second.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "second launcher did not become ready"
        assert second.stdout.readline() == "ready\n"
        assert first.poll() is None, "a different client reaped the live server"
        log = (tmp_path / ".voicemode" / "logs" / "reap.log").read_text()
        assert "not reaping" in log
        # The pidfile still names the original server, so its own client can replace it.
        pidfile = tmp_path / ".voicemode" / "run" / "voicemode-shared-session.pid"
        assert pidfile.read_text() == str(first.pid)
    finally:
        # Stop the launcher (now the fake server) under the client shell, then the shell.
        subprocess.run(["pkill", "-TERM", "-P", str(second.pid)], check=False)
        if second.poll() is None:
            second.send_signal(signal.SIGTERM)
        second.wait(timeout=5)
        second.stdout.close()
        second.stderr.close()


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


def _tools_seen(tmp_path, env_file_text=None, **env_extra):
    """Run voicemode-mcp with a fake voice-mode that prints the tool list it was given."""
    script = tmp_path / "voicemode-mcp"
    shutil.copy(REPO / "voicemode-mcp", script)
    (tmp_path / "voicemode-switch").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tmp_path / "voicemode-switch").chmod(0o755)
    server = tmp_path / ".venv" / "bin" / "voice-mode"
    server.parent.mkdir(parents=True)
    server.write_text("#!/usr/bin/env bash\necho \"tools=${VOICEMODE_TOOLS_ENABLED-unset}\"\n")
    server.chmod(0o755)
    if env_file_text is not None:
        (tmp_path / ".voicemode").mkdir(exist_ok=True)
        (tmp_path / ".voicemode" / "voicemode.env").write_text(env_file_text)
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICEMODE_TOOLS")}
    env.update(HOME=str(tmp_path), **env_extra)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    done = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
    return done.stdout.strip().splitlines()[-1]


def test_no_tool_setting_gets_voice_stop_and_barge(tmp_path):
    # Seen on the Mac 2026-11-11: no list, so upstream offered only converse and service.
    assert _tools_seen(tmp_path) == "tools=converse,service,voice_barge_tool"


@pytest.mark.parametrize("line", ["VOICEMODE_TOOLS_ENABLED=converse\n",
                                  "export VOICEMODE_TOOLS_DISABLED=service\n",
                                  "VOICEMODE_TOOLS=converse\n"])
def test_a_tool_setting_in_voicemode_env_is_left_alone(tmp_path, line):
    assert _tools_seen(tmp_path, line) == "tools=unset"


def test_a_tool_setting_in_the_environment_is_left_alone(tmp_path):
    assert _tools_seen(tmp_path, VOICEMODE_TOOLS_ENABLED="converse") == "tools=converse"
    (tmp_path / "b").mkdir()
    assert _tools_seen(tmp_path / "b", VOICEMODE_TOOLS_DISABLED="x") == "tools=unset"
    (tmp_path / "c").mkdir()   # empty counts as unset, as upstream strips it
    assert _tools_seen(tmp_path / "c", VOICEMODE_TOOLS_ENABLED="") == "tools=converse,service,voice_barge_tool"
