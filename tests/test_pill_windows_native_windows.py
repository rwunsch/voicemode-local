"""pill-windows.ps1 in native mode (no WSL) on real Windows. Windows only; no audio.

Run from WSL with tests/run_windows_transport_tests.sh. Each test starts the window the
way the launcher does: the command from voicemode_native.optional_command("pill"), from
a process with no window (CREATE_NO_WINDOW, as background_options), stdin closed. The
backend is tests/pill_fake_backend.py unless a test says otherwise. Every file goes to
pytest's tmp_path: the data folder, a private USERPROFILE, the fake's folder. The
WebView2 DLLs are copied from the WSL pill's folder when it has them (read only), else
downloaded. A window appears on screen for a few seconds per start; every test closes it.
"""
import ctypes
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

REPO = Path(__file__).resolve().parent.parent
FAKE = REPO / "tests" / "pill_fake_backend.py"
TITLE = "Voice pill"


def _load_native():
    spec = importlib.util.spec_from_file_location("vn_pill_windows", REPO / "scripts/windows/voicemode_native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if sys.platform == "win32":
    vn = _load_native()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    WSL_PILL = Path(os.environ["LOCALAPPDATA"]) / "voicemode-local"
    WEBVIEW2 = re.search(r'\$WebView2Version = "([^"]+)"', (REPO / "pill-windows.ps1").read_text()).group(1)


def windows_of(pid):
    """(hwnd, visible) of every top-level window titled TITLE that belongs to pid."""
    found = []

    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            buf = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(hwnd, buf, 64)
            if buf.value == TITLE:
                found.append((hwnd, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(WNDENUMPROC(each), 0)
    return found


def wait_visible(pill, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        shown = [h for h, visible in windows_of(pill.process.pid) if visible]
        if shown:
            return shown[0]
        if pill.process.poll() is not None:
            raise AssertionError(f"pill exited {pill.process.returncode} before showing:\n{pill.log()}")
        time.sleep(0.2)
    raise AssertionError(f"no visible {TITLE!r} window in {timeout} s:\n{pill.log()}")


def set_event(name):
    handle = kernel32.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


class Pill:
    def __init__(self, tmp_path, name, env=None, exe=None, args=None):
        self.root = tmp_path / name
        self.data = vn.pill_data_dir(self.root)
        self.fake = tmp_path / f"{name}-fake"
        self.fake.mkdir(parents=True, exist_ok=True)
        seed = WSL_PILL / "webview2" / WEBVIEW2
        if (seed / "WebView2Loader.dll").exists() and not (self.data / "webview2" / WEBVIEW2).exists():
            shutil.copytree(seed, self.data / "webview2" / WEBVIEW2)
        command, _ = vn.optional_command("pill", self.root / "runtime" / "abc", self.root)
        command[command.index("-BackendExe") + 1] = exe or sys.executable
        command[command.index("-BackendArgs") + 1] = args or subprocess.list2cmdline([str(FAKE), str(self.fake)])
        self.command = command
        self.env = {**os.environ, **(env or {})}
        for key in [k for k, v in self.env.items() if v is None]:
            del self.env[key]  # None: not set at all
        self.out = tmp_path / f"{name}-stdout.txt"
        self.process = None

    def start(self):
        with self.out.open("ab") as out:
            self.process = subprocess.Popen(self.command, env=self.env, stdin=subprocess.DEVNULL, stdout=out,
                                            stderr=out, **vn.procs.background_options())
        return self

    def log(self):
        text = ""
        for path in (self.data / "pill-window.log", self.out):
            if path.exists():
                text += f"--- {path.name}\n" + path.read_text(encoding="utf-8", errors="replace")
        return text

    def runs(self):
        path = self.fake / "runs.log"
        return path.read_text().splitlines() if path.exists() else []

    def stop(self, timeout=30):
        if self.process is None or self.process.poll() is not None:
            return self.process.returncode if self.process else None
        assert set_event(vn.pill_names(self.data)[1]), "the stop event does not exist"
        return self.process.wait(timeout=timeout)

    def kill(self):
        if self.process is not None and self.process.poll() is None:
            # vml_supervisor's kill_tree (Task 8); a Result instead of None means it could not
            # (psutil_missing in this Python): then the window, not its backend.
            if vn.procs.kill_tree(self.process.pid, vn.STOP_TIMEOUT) is not None:
                self.process.kill()


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".voicemode").mkdir(parents=True)
    return h


@pytest.fixture
def pills(tmp_path, home):
    started = []

    def make(name="install", env=None, **kwargs):
        base = {"USERPROFILE": str(home), "VOICEMODE_BASE_DIR": str(tmp_path / "vm"), "VOICEMODE_PILL": "true"}
        base.update(env or {})
        pill = Pill(tmp_path, name, base, **kwargs)
        started.append(pill)
        return pill

    yield make
    for pill in started:
        try:
            pill.stop(timeout=15)
        except Exception:
            pill.kill()


class ForegroundHelper:
    """A small window of this test's own, made the foreground window when Windows reports
    none (a locked or idle session). Windows lets a process take the foreground when there
    is no foreground window. No clicks, no keystrokes. The pill is then started by the
    foreground process, which Windows would let it take the foreground from: a harder test.
    The window lives on its own thread with a message loop: a window that pumps nothing
    blocks every program that broadcasts a message to all windows."""

    def __init__(self):
        import threading

        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.hwnd = None
        ready = threading.Event()

        def run():
            # WS_POPUP | WS_VISIBLE, a 40 px square at the top left.
            self.hwnd = user32.CreateWindowExW(0, "STATIC", "vml focus helper", 0x80000000 | 0x10000000,
                                               0, 0, 40, 40, None, None, None, None)
            if self.hwnd:
                user32.ShowWindow(self.hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(self.hwnd)
            ready.set()
            msg = wintypes.MSG()
            while self.hwnd and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0400:  # WM_USER: close, from close()
                    user32.DestroyWindow(self.hwnd)
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(10)

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, 0x0400, 0, 0)
            self.thread.join(10)
            self.hwnd = None


def window_owner(hwnd):
    """(pid, image file name, title) of a window."""
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    title = ctypes.create_unicode_buffer(128)
    user32.GetWindowTextW(hwnd, title, 128)
    image = ""
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, owner.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            image = Path(buf.value).name
        kernel32.CloseHandle(handle)
    return owner.value, image, title.value


def test_shows_without_taking_focus_when_started_hidden(pills):
    """At least 3 measured starts: a start counts only when a real window had the
    foreground before it. With none, the helper window is made foreground first; if even
    that fails, the start is not measured. A start where the foreground moved to some
    other program (the user at work) is not measured either; a move to the pill, its
    WebView2 or its backend fails."""
    measured, attempts = 0, 0
    while measured < 3 and attempts < 8:
        n, attempts = attempts, attempts + 1
        helper = None
        before = user32.GetForegroundWindow()
        if not before:
            helper = ForegroundHelper()
            time.sleep(0.3)
            before = user32.GetForegroundWindow()
        try:
            t0 = time.monotonic()
            pill = pills(f"focus{n}").start()
            hwnd = wait_visible(pill)
            shown = time.monotonic() - t0
            time.sleep(1.0)  # past the first page layout and the backend's first snapshots
            after = user32.GetForegroundWindow()
            print(f"\nstart {n}: visible after {shown:.1f} s; pill {hwnd}, foreground {before} -> {after}"
                  f"{' (helper)' if helper else ''}{'' if before else ' NOT MEASURED'}")
            assert after != hwnd, f"start {n}: the pill took the foreground"
            if before and after != before:
                pid, image, title = window_owner(after)
                print(f"start {n}: foreground moved to pid {pid} {image} {title!r}")
                ours = pid == pill.process.pid or image.lower() in ("msedgewebview2.exe", Path(sys.executable).name.lower())
                assert not ours, f"start {n}: the foreground moved to the pill's own process tree: {image}"
            elif before:
                measured += 1
            assert pill.stop() == 0, pill.log()
            # Shutdown closes the backend's stdin: the fake records the EOF.
            assert any("stdin EOF" in line for line in pill.runs()), pill.log()
            assert windows_of(pill.process.pid) == []
        finally:
            if helper:
                helper.close()
    assert measured >= 3, f"only {measured} of {attempts} starts had a foreground window to keep"


def test_files_land_in_the_data_dir_and_not_in_the_wsl_folder(pills, tmp_path):
    before = set(os.listdir(WSL_PILL)) if WSL_PILL.exists() else set()
    pill = pills().start()
    wait_visible(pill)
    assert pill.stop() == 0
    for name in ("pill-window.log", "ui", "webview2", "webview2-data"):
        assert (pill.data / name).exists(), name
    assert (pill.data / "ui" / "index.html").exists()
    after = set(os.listdir(WSL_PILL)) if WSL_PILL.exists() else set()
    assert after == before
    wsl_log = WSL_PILL / "pill-window.log"
    if wsl_log.exists():
        assert str(pill.data) not in wsl_log.read_text(encoding="utf-8", errors="replace")
    assert "native backend=" in pill.log()


def test_a_second_start_on_the_same_dir_exits_and_another_dir_runs_alongside(pills):
    first = pills("one").start()
    wait_visible(first)
    again = pills("one")
    again.start()
    assert again.process.wait(timeout=60) == 0
    assert "another pill window is running" in first.log()
    other = pills("two").start()
    wait_visible(other)
    assert first.process.poll() is None and other.process.poll() is None
    assert first.stop() == 0
    assert other.process.poll() is None  # its own stop event, its own mutex
    assert other.stop() == 0


def test_a_crashed_backend_is_restarted(pills):
    # Exit 2: a crash. Not 3, which since d026ecb (2026-11-11) is voice_pill.EXIT_DONE, "the last
    # voice session has left", on which the window closes and does not restart (final review M8:
    # this test still used 3 and failed from then on).
    pill = pills().start()
    wait_visible(pill)
    (pill.fake / "exit_now").write_text("2")
    wait_for(lambda: sum("pid=" in line for line in pill.runs()) >= 2, 30, "a second backend run")
    assert "backend exited: code 2" in pill.log()
    assert pill.stop() == 0


def test_exit_done_closes_the_window_without_a_restart(pills):
    pill = pills().start()
    wait_visible(pill)
    (pill.fake / "exit_now").write_text(str(vn_pill_exit_done()))
    assert pill.process.wait(timeout=60) == 0, pill.log()
    assert "no voice session left; closing" in pill.log()
    assert sum("pid=" in line for line in pill.runs()) == 1


def vn_pill_exit_done():
    """voice_pill.EXIT_DONE, read from the patch source (it imports voice-mode, so it is not loaded)."""
    text = (REPO / "patches" / "voice_pill.py").read_text(encoding="utf-8")
    return int(re.search(r"^EXIT_DONE = (\d+)", text, re.M).group(1))


def test_a_backend_that_refuses_before_any_output_ends_the_window(pills):
    pill = pills()
    (pill.fake / "startup_exit").write_text("1")
    pill.start()
    assert pill.process.wait(timeout=90) == 1, pill.log()
    assert "has never run; exiting" in pill.log()


def _env_file(home, data):
    (home / ".voicemode" / "voicemode.env").write_bytes(data)


def test_setting_off_in_the_env_file_exits_before_showing(pills, home):
    _env_file(home, b"VOICEMODE_PILL=true\r\nVOICEMODE_PILL=false\r\n")  # the last one wins
    pill = pills(env={"VOICEMODE_PILL": None})
    pill.start()
    assert pill.process.wait(timeout=60) == 0
    assert "VOICEMODE_PILL is not true; exiting" in pill.log()
    assert pill.runs() == []


def test_setting_on_in_a_crlf_utf8_bom_env_file_runs(pills, home):
    _env_file(home, "\ufeff# J\u00fcrgen\r\nexport VOICEMODE_PILL=\"true\"\r\n".encode("utf-8"))
    pill = pills(env={"VOICEMODE_PILL": None}).start()
    wait_visible(pill)
    assert pill.stop() == 0


def test_the_environment_wins_over_the_env_file(pills, home):
    _env_file(home, b"VOICEMODE_PILL=true\n")
    pill = pills(env={"VOICEMODE_PILL": "false"})
    pill.start()
    assert pill.process.wait(timeout=60) == 0
    assert "VOICEMODE_PILL is not true; exiting" in pill.log()


def _native_python():
    root = Path(os.environ["LOCALAPPDATA"]) / "voicemode-local-native"
    try:
        import json
        name = json.loads((root / "active.json").read_text(encoding="utf-8"))["runtime"]
    except (OSError, ValueError, KeyError):
        return None
    python = root / "runtime" / name / ".venv" / "Scripts" / "python.exe"
    return python if python.exists() else None


def test_the_real_backend_runs_and_ends_on_eof(pills, tmp_path):
    python = _native_python()
    if python is None:
        pytest.skip("no native runtime in %LOCALAPPDATA%\\voicemode-local-native")
    pill = pills("real", exe=str(python), args="-m voice_mode.voice_pill --stdio").start()
    # voice_pill takes its single-instance lock in the private base dir once imported.
    wait_for(lambda: (tmp_path / "vm" / "pill" / "backend.lock").exists(), 90, "the backend's lock file")
    time.sleep(2)
    assert "backend exited" not in pill.log(), pill.log()
    assert pill.stop() == 0
    assert "backend exited" not in pill.log()  # closing: ended by EOF, no restart


# -- names: every spelling of one folder gives one name ------------------------

def print_names(data_dir, cwd):
    out = subprocess.run([vn.powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          str(REPO / "pill-windows.ps1"), "-BackendExe", "C:\\none\\python.exe",
                          "-DataDir", str(data_dir), "-PrintNames"],
                         capture_output=True, cwd=cwd, timeout=60, **vn.procs.background_options())
    assert out.returncode == 0, out
    return tuple(line.strip() for line in out.stdout.decode("ascii").splitlines() if line.strip())


def test_a_relative_data_dir_names_agree(tmp_path, monkeypatch):
    (tmp_path / "rel").mkdir()
    monkeypatch.chdir(tmp_path)
    names = print_names("rel\\pill", tmp_path)
    assert names == vn.pill_names("rel\\pill") == vn.pill_names(tmp_path / "rel" / "pill")


def _short(path):
    kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    buf = ctypes.create_unicode_buffer(1024)
    n = kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if n else None


def test_an_8dot3_short_name_gives_the_long_names_name(tmp_path):
    long_dir = tmp_path / "A Long Folder Name For VML"
    long_dir.mkdir()
    short = _short(long_dir)
    if not short or Path(short).name.lower() == long_dir.name.lower():
        pytest.skip("8.3 short names are disabled on this volume")
    want = vn.pill_names(long_dir / "pill")
    # The folder that holds the data dir exists; pill itself need not, as on a first start.
    for spelling in (short + "\\pill", short + "\\pill\\"):
        assert vn.pill_names(spelling) == want, spelling
        assert print_names(spelling, tmp_path) == want, spelling
    (long_dir / "pill").mkdir()
    assert vn.pill_names(short + "\\pill") == want
    assert print_names(short + "\\pill", tmp_path) == want
