"""pill-windows.ps1's native mode (no WSL), checked from Linux.

Static checks of the script, and one check that runs it: where powershell.exe is
reachable (WSL), the names the script derives with -PrintNames must equal
voicemode_native.pill_names(). The window itself runs in
tests/test_pill_windows_native_windows.py, on Windows.
"""
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PS1 = REPO / "pill-windows.ps1"
TEXT = PS1.read_text(encoding="utf-8")


def _native():
    spec = importlib.util.spec_from_file_location("vn_pill_names", REPO / "scripts/windows/voicemode_native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _csharp_method(name):
    start = TEXT.index(name)
    body = TEXT[TEXT.index("{", start):]
    depth = 0
    for i, ch in enumerate(body):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            return body[:i + 1]
    raise AssertionError(f"unbalanced {name}")


def test_script_is_ascii():
    # Windows PowerShell 5.1 reads a file without a BOM as the ANSI code page.
    TEXT.encode("ascii")


def test_wsl_mode_keeps_its_names_and_folder_byte_for_byte():
    # voicemode-switch:508 signals the stop event by this name; tests pin the folder.
    assert '"Local\\VoiceModePillWindow"' in TEXT
    assert '"Local\\VoiceModePillStop"' in TEXT
    assert 'Join-Path $env:LOCALAPPDATA "voicemode-local"' in TEXT
    switch = (REPO / "voicemode-switch").read_text(encoding="utf-8")
    assert "TryOpenExisting('Local\\\\VoiceModePillStop'" in switch


def test_native_mode_has_its_own_parameters():
    for name in ("BackendExe", "BackendArgs", "DataDir", "PrintNames"):
        assert re.search(r"\[(string|switch)\]\$" + name + r"\b", TEXT), name


def test_native_mode_is_chosen_without_a_distro():
    assert re.search(r"\$Native\s*=\s*\[bool\]\$BackendExe\s+-or\s+\(-not \$Distro -and -not \$OnWsl\)", TEXT)


def test_native_backend_starts_without_wsl_or_python3():
    start = _csharp_method("void StartBackend()")
    native = start[start.index("if (native)"):start.index("else")]
    assert "new ProcessStartInfo(exe, args)" in native
    assert "wsl" not in native.lower() and "python3" not in native


def test_native_pill_setting_is_read_in_process_without_wsl():
    method = _csharp_method("public static int NativePillEnabled(")
    assert "wsl" not in method.lower() and "Process" not in method
    assert 'GetEnvironmentVariable("VOICEMODE_PILL")' in method
    assert "UTF8" in method and "voicemode.env" in method and ".voicemode.env" in method
    assert method.index("GetEnvironmentVariable") < method.index("ReadAllText")


def test_stop_event_name_is_passed_in_not_fixed_in_csharp():
    main = _csharp_method("int Main(")
    assert "new EventWaitHandle(false, EventResetMode.AutoReset, stopName)" in main


def test_mutex_uses_the_mode_name():
    assert re.search(r"New-Object Threading\.Mutex\(\$true, \$MutexName, \[ref\]\$created\)", TEXT)


@pytest.mark.skipif(shutil.which("powershell.exe") is None or shutil.which("wslpath") is None,
                    reason="needs powershell.exe (WSL)")
@pytest.mark.parametrize("path", [
    "C:\\Users\\Me\\AppData\\Local\\voicemode-local-native\\pill",
    "C:\\Users\\J\u00fcrgen M\u00fcller\\Local\\vml native\\pill",
    "D:\\Tools\\VML\\pill\\",
    # Other spellings of one folder: GetFullPath normalises them, so pill_names must too.
    "C:/Users/me/vml/pill",
    "C:\\Users\\me\\vml\\..\\vml\\pill",
    "C:\\Users\\me\\.\\vml\\pill",
    "C:\\Users\\me\\vml\\pill. ",
    "C:\\Users\\me\\vml\\pill...",
    "\\\\server\\share\\a\\..\\pill",
])
def test_powershell_and_python_derive_the_same_names(path):
    script = subprocess.run(["wslpath", "-w", str(PS1)], capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
         "-BackendExe", "C:\\none\\python.exe", "-DataDir", path, "-PrintNames"],
        capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    lines = [line.strip() for line in out.stdout.decode("ascii").splitlines() if line.strip()]
    assert tuple(lines) == _native().pill_names(path)


@pytest.mark.skipif(shutil.which("powershell.exe") is None or shutil.which("wslpath") is None,
                    reason="needs powershell.exe (WSL)")
def test_wsl_mode_prints_todays_names():
    script = subprocess.run(["wslpath", "-w", str(PS1)], capture_output=True, text=True, check=True).stdout.strip()
    out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                          "-PrintNames"], capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    lines = [line.strip() for line in out.stdout.decode("ascii").splitlines() if line.strip()]
    assert lines == ["Local\\VoiceModePillWindow", "Local\\VoiceModePillStop"]


@pytest.mark.parametrize("spelling", [
    "C:/Users/me/vml/pill",
    "C:\\Users\\me\\vml\\..\\vml\\pill",
    "C:\\Users\\me\\.\\vml\\pill",
    "C:\\Users\\me\\vml\\pill\\",
    "C:\\Users\\me\\vml\\pill. ",
    "c:\\USERS\\ME\\VML\\PILL",
])
def test_every_spelling_of_one_folder_gives_one_name(spelling):
    vn = _native()
    assert vn.pill_names(spelling) == vn.pill_names("C:\\Users\\me\\vml\\pill")


def _run_ps1(*args):
    script = subprocess.run(["wslpath", "-w", str(PS1)], capture_output=True, text=True, check=True).stdout.strip()
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, *args],
                          capture_output=True, timeout=60)


needs_powershell = pytest.mark.skipif(shutil.which("powershell.exe") is None or shutil.which("wslpath") is None,
                                      reason="needs powershell.exe (WSL)")


@needs_powershell
def test_a_refused_start_reaches_the_log_when_a_folder_is_known(tmp_path):
    data = tmp_path / "pill"
    win = subprocess.run(["wslpath", "-w", str(data)], capture_output=True, text=True, check=True).stdout.strip()
    out = _run_ps1("-Distro", "Ubuntu", "-BackendExe", "C:\\none\\python.exe", "-DataDir", win)
    assert out.returncode == 1, out
    assert b"not both" in out.stderr
    assert "not both" in (data / "pill-window.log").read_text(encoding="utf-8-sig")


@needs_powershell
def test_native_without_a_data_dir_is_refused_on_stderr():
    # No folder is known, so there is no log to write to.
    out = _run_ps1("-BackendExe", "C:\\none\\python.exe", "-Force")
    assert out.returncode == 1 and b"needs -DataDir" in out.stderr, out
