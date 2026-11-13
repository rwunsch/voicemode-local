"""The voice server starts the pill window itself (patches/pill_autostart.py)."""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
sys.path.insert(0, str(PATCHES))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("voice_pill_state")
pa = _load("pill_autostart")

SETTINGS = ("VOICEMODE_PILL", "VOICEMODE_PILL_AUTOSTART", "VOICEMODE_PILL_DATA_DIR",
            "VOICEMODE_PILL_PYTHON", "VOICEMODE_NATIVE_ROOT", "VOICEMODE_BASE_DIR")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Settings from the environment only; a home with no voicemode.env."""
    for key in SETTINGS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(st, "upstream_home", lambda: tmp_path / "home" / ".voicemode")
    monkeypatch.setenv("VOICEMODE_PILL", "true")
    monkeypatch.setattr(pa, "platform_kind", lambda: "macos")
    monkeypatch.setenv("VOICEMODE_PILL_PYTHON", sys.executable)   # a pill venv that exists
    started = []
    monkeypatch.setattr(pa, "spawn", lambda cmd, kind, base=None: started.append((kind, cmd)))
    return tmp_path / "pill", started


def test_starts_when_on_and_nothing_is_up(env):
    base, started = env
    assert pa.ensure(base) is True
    assert len(started) == 1 and started[0][0] == "macos"


def test_never_starts_with_the_pill_off(env, monkeypatch):
    base, started = env
    monkeypatch.setenv("VOICEMODE_PILL", "false")
    assert pa.ensure(base) is False and started == []


@pytest.mark.parametrize("value", ["off", "OFF", " off "])
def test_autostart_off_never_starts(env, monkeypatch, value):
    base, started = env
    monkeypatch.setenv("VOICEMODE_PILL_AUTOSTART", value)
    assert pa.ensure(base) is False and started == []


def test_a_user_close_sticks(env):
    base, started = env
    st.set_pill_closed(True, base)
    assert pa.ensure(base) is False and started == []


def test_not_while_a_backend_is_alive(env):
    base, started = env
    st._write_json(st._base(base) / "backend.json",
                   {"pid": os.getpid(), "start": st.process_start(os.getpid())})
    assert pa.ensure(base) is False and started == []


def test_a_dead_backend_file_does_not_block(env):
    base, started = env
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    st._write_json(st._base(base) / "backend.json", {"pid": p.pid, "start": "gone"})
    assert pa.ensure(base) is True


def test_two_sessions_at_once_start_one_window(env):
    base, started = env
    assert pa.ensure(base) is True
    assert pa.ensure(base) is False
    assert len(started) == 1


def test_starts_again_after_the_spacing(env, monkeypatch):
    base, started = env
    pa.ensure(base)
    monkeypatch.setattr(pa, "LAUNCH_SPACING_S", 0.0)
    assert pa.ensure(base) is True and len(started) == 2


def test_no_platform_no_start(env, monkeypatch):
    base, started = env
    monkeypatch.setattr(pa, "platform_kind", lambda: None)
    assert pa.ensure(base) is False and started == []


# -- commands -----------------------------------------------------------------

def test_windows_command(env, monkeypatch):
    base, _ = env
    monkeypatch.setenv("VOICEMODE_PILL_DATA_DIR", r"C:\FELIX\voice\pill-window")
    monkeypatch.setenv("VOICEMODE_PILL_PYTHON", r"C:\FELIX\runtime\Scripts\python.exe")
    cmd = pa.start_command("windows", base)
    assert cmd[0].endswith("powershell.exe") and "-STA" in cmd
    assert cmd[cmd.index("-File") + 1] == str(pa.HERE / "pill-windows.ps1")
    assert cmd[cmd.index("-BackendExe") + 1] == r"C:\FELIX\runtime\Scripts\python.exe"
    assert cmd[cmd.index("-BackendArgs") + 1] == "-m voice_mode.voice_pill --stdio"
    assert cmd[cmd.index("-DataDir") + 1] == r"C:\FELIX\voice\pill-window"


def test_windows_data_dir_defaults(env, monkeypatch):
    base, _ = env
    monkeypatch.setenv("VOICEMODE_NATIVE_ROOT", "/native/root")
    assert pa.data_dir(base) == Path("/native/root") / "pill"   # today's native installs
    monkeypatch.delenv("VOICEMODE_NATIVE_ROOT")
    assert pa.data_dir(base) == st._base(base) / "window"


def test_backend_python_defaults_to_this_interpreter(env):
    assert pa.backend_python() == sys.executable


def test_macos_command(env, monkeypatch):
    base, _ = env
    monkeypatch.setenv("VOICEMODE_PILL_PYTHON", "/felix/pill-venv/bin/python")
    cmd = pa.start_command("macos", base)
    assert cmd[0] == "/felix/pill-venv/bin/python"
    assert cmd[1] == str(pa.HERE / "voice_pill_mac.py")
    assert cmd[2] == "--backend" and str(pa.HERE / "voice_pill.py") in cmd[3]


def test_macos_without_a_pill_venv_does_not_start(env, monkeypatch, tmp_path):
    base, started = env
    monkeypatch.setenv("VOICEMODE_PILL_PYTHON", str(tmp_path / "no-venv" / "python"))
    assert pa.ensure(base) is False and started == []


def test_macos_python_default(env, monkeypatch):
    monkeypatch.delenv("VOICEMODE_PILL_PYTHON")
    assert pa.mac_python() == str(Path.home() / ".voicemode" / "pill-venv" / "bin" / "python")


def test_wsl_command_goes_through_start_process(env, monkeypatch):
    base, _ = env
    monkeypatch.setattr(pa, "_wsl_windows_path", lambda p: r"\\wsl.localhost\Ubuntu" + str(p).replace("/", "\\"))
    cmd = pa.start_command("wsl", base)
    assert cmd[:3] == ["powershell.exe", "-NoProfile", "-Command"]
    assert cmd[3].startswith("Start-Process powershell.exe -WindowStyle Hidden -ArgumentList ")
    assert "pill-windows.ps1" in cmd[3] and "-BackendCommand" in cmd[3]
    assert "voice_pill.py" in cmd[3] and "--stdio" in cmd[3]


# -- spawn ------------------------------------------------------------------

def _wait_gone(pid, timeout=10):
    """A detached window is no child of ours: no Popen to wait on, only its pid."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX detach")


@posix_only
def test_spawn_detaches_and_logs(tmp_path):
    """FELIX-MAC, 2026-11-13: the app kills a deleted chat's processes by ancestry, so its own session
    is not enough: the window starts through a middle process and is no child of the server."""
    base = tmp_path / "pill"
    pid = pa.spawn([sys.executable, "-c", "import os; print(os.getsid(0) == os.getpid(), os.getppid())"],
                   "wsl", base)
    assert isinstance(pid, int) and _wait_gone(pid)
    log = (st._base(base) / "autostart.log").read_text()
    session, parent = log.splitlines()[-1].split()
    assert "start:" in log and session == "True"   # its own session: not killed with the server's group
    assert int(parent) != os.getpid()               # and not our child: not killed with the chat


@posix_only
def test_mac_window_logs_to_pill_window_log(tmp_path, monkeypatch):
    base = tmp_path / "pill"
    monkeypatch.setattr(st, "upstream_home", lambda: tmp_path / "home" / ".voicemode")
    pid = pa.spawn([sys.executable, "-c", "import os, sys; print('hb' + ' line', os.getppid(), file=sys.stderr)"],
                   "macos", base)
    assert isinstance(pid, int) and _wait_gone(pid)
    text = (tmp_path / "home" / ".voicemode" / "logs" / "pill-window.log").read_text()
    assert "hb line" in text and int(text.split()[-1]) != os.getpid()
    log = (st._base(base) / "autostart.log").read_text()
    assert "start:" in log and "hb line" not in log


@posix_only
@pytest.mark.parametrize("kind", ["wsl", "macos"])
def test_a_window_that_cannot_start_is_logged_not_raised(tmp_path, monkeypatch, kind):
    base = tmp_path / "pill"
    monkeypatch.setattr(st, "upstream_home", lambda: tmp_path / "home" / ".voicemode")
    assert pa.spawn([str(tmp_path / "no-such-window")], kind, base) is None
    log = (st._base(base) / "autostart.log").read_text()
    assert "start failed:" in log and "no-such-window" in log


# -- wiring -------------------------------------------------------------------

def test_default_hook_is_off_outside_the_package():
    assert st._default_autostart() is None


def test_pill_python_from_a_voicemode_env_is_refused(monkeypatch):
    # Talos 2026-11-12: a model can write voicemode.env (update_config), and the server
    # copies it into its environment; the pill would launch whatever it names.
    monkeypatch.setenv(pa.PYTHON_SETTING, "/tmp/evil")
    monkeypatch.setattr(st, "setting_came_from_a_file", lambda name: name == pa.PYTHON_SETTING)
    assert pa.backend_python() == sys.executable
    assert pa.mac_python().endswith(os.path.join("pill-venv", "bin", "python"))


def test_pill_python_from_the_environment_is_used(monkeypatch):
    monkeypatch.setenv(pa.PYTHON_SETTING, "/opt/py")
    monkeypatch.setattr(st, "setting_came_from_a_file", lambda name: False)
    assert pa.backend_python() == "/opt/py" and pa.mac_python() == "/opt/py"
