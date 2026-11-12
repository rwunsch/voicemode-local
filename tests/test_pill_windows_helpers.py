"""pill-windows.ps1 behaviour checked from WSL through Windows PowerShell.

The C# helpers are cut out of the script, compiled on their own with Add-Type and
called; -InstallAutostart is run for real into a temporary folder, and the .cmd it
writes is then run with cmd.exe against a stand-in script that records what it got.
Needs powershell.exe and wslpath (WSL); skipped elsewhere. Every file goes under
C:\\Temp or pytest's tmp_path; nothing reads or writes ~/.voicemode except the one
read-only test that compares the WSL setting with Python's reading of it.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PS1 = REPO / "pill-windows.ps1"
TEXT = PS1.read_text(encoding="utf-8")
WIN_TEMP = Path("/mnt/c/Temp")

pytestmark = pytest.mark.skipif(shutil.which("powershell.exe") is None or shutil.which("wslpath") is None,
                                reason="needs powershell.exe (WSL)")

USINGS = """using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
"""


def method(signature):
    """The whole C# member that starts with signature, through its closing brace."""
    start = TEXT.index(signature)
    start = TEXT.rindex("\n", 0, start) + 1
    body = TEXT.index("{", start)
    depth = 0
    for i in range(body, len(TEXT)):
        depth += TEXT[i] == "{"
        depth -= TEXT[i] == "}"
        if depth == 0:
            return TEXT[start:i + 1]
    raise AssertionError(f"unbalanced {signature}")


def winpath(p):
    return subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True, check=True).stdout.strip()


def powershell(command, timeout=120):
    return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, timeout=timeout)


def run_csharp(tmp_path, signatures, calls):
    """Compile the named members into class T, then run calls (PowerShell, $T is the type)."""
    name = "VmlT" + uuid.uuid4().hex[:8]
    src = tmp_path / f"{name}.cs"
    src.write_text(USINGS + f"public class {name} {{\n" + "\n".join(method(s) for s in signatures) + "\n}\n",
                   encoding="utf-8")
    out = powershell("$ErrorActionPreference='Stop'; Set-Location $env:TEMP; "
                     f"Add-Type -ReferencedAssemblies System.Web.Extensions -TypeDefinition "
                     f"([IO.File]::ReadAllText('{winpath(src)}')); $T = [{name}]; " + calls)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    return out.stdout.decode("utf-8", "replace").splitlines()


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# -- fix 1: the WSL check never blocks past its timeout ----------------------

OUTPUT = ["public static string Output("]


def _timed(tmp_path, file, arguments, timeout_ms):
    lines = run_csharp(tmp_path, OUTPUT, (
        "$w = [Diagnostics.Stopwatch]::StartNew(); "
        f"$o = $T::Output('{file}', '{arguments}', {timeout_ms}); "
        "if ($o -eq $null) { 'NULL' } else { 'OUT:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($o)) }; "
        "[int]$w.Elapsed.TotalMilliseconds"))
    result, ms = lines[-2], int(lines[-1])
    text = None if result == "NULL" else base64.b64decode(result[4:]).decode("utf-8")
    return text, ms


def test_output_returns_what_the_program_printed(tmp_path):
    text, _ = _timed(tmp_path, "cmd.exe", "/c echo hello", 10000)
    assert text is not None and text.strip() == "hello"


def test_output_gives_up_on_a_program_that_outlasts_the_timeout(tmp_path):
    # ping -n 15 runs about 14 s; ReadToEnd would wait for all of it.
    text, ms = _timed(tmp_path, "cmd.exe", "/c ping -n 15 127.0.0.1 >nul", 1500)
    assert text is None
    assert ms < 8000, f"took {ms} ms: the timeout did not apply"


def test_output_gives_up_when_a_child_keeps_stdout_open(tmp_path):
    # cmd exits at once; the ping it started holds the pipe, as a stuck wsl.exe child would.
    text, ms = _timed(tmp_path, "cmd.exe", "/c start /b ping -n 15 127.0.0.1", 1500)
    assert text is None
    assert ms < 8000, f"took {ms} ms: the open pipe blocked the read"


def test_pill_enabled_reads_through_output_with_a_timeout():
    enabled = method("public static int PillEnabled(")
    assert "Output(\"wsl.exe\"" in enabled and "ReadToEnd()" not in enabled
    assert "ReadToEnd()" not in method("public static string Output(")


# -- fix 2: the WSL setting is read by the same rules as natively ------------

SETTING_TABLE = [
    ("VOICEMODE_PILL=true\n", 1),
    ('VOICEMODE_PILL="true"\n', 1),
    ("VOICEMODE_PILL='true'\n", 1),
    ("export VOICEMODE_PILL=true\n", 1),
    ("VOICEMODE_PILL=true\r\n", 1),
    ("  VOICEMODE_PILL = true  \r\n", 1),
    ("VOICEMODE_PILL=true\nVOICEMODE_PILL=false\n", 0),   # the last one wins
    ("VOICEMODE_PILL=false\nexport VOICEMODE_PILL=\"true\"\r\n", 1),
    ("VOICEMODE_PILL_AUTOSTART=true\n", 0),               # another key
    ("# VOICEMODE_PILL=true\n", 0),
    ("\ufeffVOICEMODE_PILL=true\n", 1),
    ("", 0),
    # voice_pill_state._parse_env (and upstream's config) keep an inline comment in the
    # value, so Python reads this as off; the window must agree with the backend.
    ("VOICEMODE_PILL=true # on\n", 0),
]


@pytest.mark.parametrize("text,want", SETTING_TABLE)
def test_python_reads_the_table_the_same_way(text, want):
    sys.path.insert(0, str(REPO / "patches"))
    import voice_pill_state as st
    got = 1 if st._parse_env(text.lstrip("\ufeff")).get("VOICEMODE_PILL", "").strip() == "true" else 0
    assert got == want


def test_wsl_setting_uses_the_native_rules(tmp_path):
    calls = "; ".join(
        f"$T::WslPillSetting([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64(t)}')))"
        for t, _ in SETTING_TABLE)
    lines = run_csharp(tmp_path, ["public static int WslPillSetting(", "public static int PillSettingIn("], calls)
    assert [int(x) for x in lines] == [w for _, w in SETTING_TABLE]


def test_native_setting_uses_the_same_parser():
    native = method("public static int NativePillEnabled(")
    assert "PillSettingIn(" in native


@pytest.mark.skipif(not os.environ.get("WSL_DISTRO_NAME"), reason="needs to run inside WSL")
def test_pill_enabled_agrees_with_python_on_this_distro(tmp_path):
    # Read only: this distro's real ~/.voicemode/voicemode.env.
    sys.path.insert(0, str(REPO / "patches"))
    import voice_pill_state as st
    home = Path.home() / ".voicemode"
    path = next((p for p in (home / "voicemode.env", home / ".voicemode.env") if p.exists()), None)
    text = path.read_text(encoding="utf-8-sig") if path else ""
    want = 1 if st._parse_env(text).get("VOICEMODE_PILL", "").strip() == "true" else 0
    lines = run_csharp(tmp_path, ["public static int PillEnabled(", "public static int WslPillSetting(",
                                  "public static int PillSettingIn(", "public static string Output("],
                       f"$T::PillEnabled('{os.environ['WSL_DISTRO_NAME']}')")
    assert int(lines[-1]) == want


# -- fix 4: a command the backend cannot take is reported to the page -------

def test_send_reports_a_missing_or_broken_backend(tmp_path):
    lines = run_csharp(tmp_path, ["public static bool Send("], (
        "$T::Send($null, 'x'); "
        "$m = New-Object IO.MemoryStream; $w = New-Object IO.StreamWriter($m); $w.AutoFlush = $true; "
        "$T::Send($w, '{\"cmd\":\"stop\"}'); $w.Close(); $T::Send($w, 'x'); "
        "$s = New-Object IO.Pipes.AnonymousPipeServerStream([IO.Pipes.PipeDirection]::Out); "
        "$c = New-Object IO.Pipes.AnonymousPipeClientStream([IO.Pipes.PipeDirection]::In, $s.ClientSafePipeHandle); "
        "$c.Dispose(); $s.DisposeLocalCopyOfClientHandle(); "
        "$p = New-Object IO.StreamWriter($s); $p.AutoFlush = $true; $T::Send($p, 'x')"))
    assert lines == ["False", "True", "False", "False"]


def test_dropped_message_shape(tmp_path):
    lines = run_csharp(tmp_path, ["public static string Dropped("],
                       "$T::Dropped('stop'); $T::Dropped('say \"hi\"'); $T::Dropped($null)")
    assert [json.loads(x) for x in lines] == [
        {"window": {"dropped": "stop"}}, {"window": {"dropped": 'say "hi"'}}, {"window": {"dropped": ""}}]


def test_a_dropped_command_is_posted_back_to_the_page():
    page = method("void OnPageMessage(")
    assert "Send(stdin, text)" in page
    assert "PostWebMessageAsString(Dropped(" in page


# -- fix 3: -InstallAutostart keeps the arguments, and non-ASCII paths -------

STUB = r"""param([string]$Distro, [string]$Repo, [string]$BackendCommand, [string]$BackendExe,
      [string]$BackendArgs, [string]$DataDir, [switch]$Force)
$o = [ordered]@{ script = $MyInvocation.MyCommand.Path }
foreach ($k in $PSBoundParameters.Keys) { $o[$k] = [string]$PSBoundParameters[$k] }
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'got.json'), ($o | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
"""


@pytest.fixture
def win_dir():
    if not WIN_TEMP.is_dir():
        pytest.skip("no C:\\Temp")
    d = WIN_TEMP / f"vml-autostart-{uuid.uuid4().hex[:8]}" / "J\u00fcrgen M\u00fcller \u0416"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d.parent, ignore_errors=True)


def _install_and_run(win_dir, *args):
    """Install from a copy of the script in win_dir, swap the copy for STUB, run the .cmd."""
    script = win_dir / "pill-windows.ps1"
    shutil.copyfile(PS1, script)
    startup = win_dir / "Startup"
    startup.mkdir()
    out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", winpath(script),
                          "-InstallAutostart", "-StartupDir", winpath(startup), *args],
                         capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    entry = startup / "voicemode-pill.cmd"
    raw = entry.read_bytes()
    script.write_text(STUB, encoding="ascii")
    got = win_dir / "got.json"
    subprocess.run(["cmd.exe", "/c", winpath(entry)], capture_output=True, timeout=60, cwd="/mnt/c/Temp")
    deadline = time.monotonic() + 30
    while not got.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert got.exists(), raw.decode("utf-8", "replace")
    return raw, json.loads(got.read_text(encoding="utf-8")), script


def test_autostart_keeps_native_arguments_and_non_ascii_paths(win_dir):
    data = win_dir / "data dir"
    backend_args = '"C:\\Program Files\\x y\\pill.py" --stdio & echo 100%'
    raw, got, script = _install_and_run(
        win_dir, "-BackendExe", "C:\\Users\\J\u00fcrgen\\py thon.exe", "-BackendArgs", backend_args,
        "-DataDir", winpath(data), "-Force")
    assert not raw.startswith(b"\xef\xbb\xbf")          # no BOM: cmd would run it as a command
    assert raw.startswith(b"@chcp 65001>nul\r\n")
    raw.decode("utf-8")
    assert got.pop("script") == winpath(script)
    assert got == {"BackendExe": "C:\\Users\\J\u00fcrgen\\py thon.exe", "BackendArgs": backend_args,
                   "DataDir": winpath(data), "Force": "True"}


def test_autostart_keeps_backend_args_that_start_with_a_dash(win_dir):
    # patches/pill_autostart.py passes exactly this; -File must not take "-m" for a parameter.
    data = win_dir / "data"
    _, got, _ = _install_and_run(win_dir, "-BackendExe", "C:\\py\\python.exe",
                                 "-BackendArgs", "-m voice_mode.voice_pill --stdio", "-DataDir", winpath(data))
    got.pop("script")
    assert got == {"BackendExe": "C:\\py\\python.exe", "BackendArgs": "-m voice_mode.voice_pill --stdio",
                   "DataDir": winpath(data)}


def test_autostart_keeps_wsl_arguments(win_dir):
    command = '"/home/me/my repo/.venv/bin/python" "/home/me/my repo/patches/voice_pill.py" --stdio'
    data = win_dir / "data"   # a folder of the test's own, so the real pill log is not written
    raw, got, _ = _install_and_run(win_dir, "-Distro", "Ubuntu-22.04", "-Repo", "/home/me/my repo",
                                   "-BackendCommand", command, "-DataDir", winpath(data))
    got.pop("script")
    assert got == {"Distro": "Ubuntu-22.04", "Repo": "/home/me/my repo", "BackendCommand": command,
                   "DataDir": winpath(data)}
