"""ptt-listener-windows.ps1's native mode (no WSL relay), checked from Linux.

Static checks of the script, and checks that run it where powershell.exe is reachable
(WSL): the native and relay parameters cannot be mixed, and a sender that cannot start
fails plainly. Nothing here presses a key: -SendAction calls the listener's send
function directly. The sender (patches/ptt_control_client.py --stdin) is tested in
tests/test_ptt_control_client.py; the real named pipe in
tests/test_ptt_listener_windows_native_windows.py, on Windows.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PS1 = REPO / "ptt-listener-windows.ps1"
TEXT = PS1.read_text(encoding="utf-8")

needs_powershell = pytest.mark.skipif(shutil.which("powershell.exe") is None or shutil.which("wslpath") is None,
                                      reason="needs powershell.exe (WSL)")


def test_script_is_ascii():
    # Windows PowerShell 5.1 reads a file without a BOM as the ANSI code page.
    TEXT.encode("ascii")


def test_relay_mode_keeps_its_defaults_and_wire_format():
    assert re.search(r"\[int\]\$Port = 8765\b", TEXT)
    assert re.search(r'\[string\]\$RelayHost = "127\.0\.0\.1"', TEXT)
    # -HoldThresholdMs is still accepted (an old shortcut may pass it) but no longer used.
    assert re.search(r"\[int\]\$HoldThresholdMs = 0\b", TEXT)
    # The WSL log file stays the default when no -LogPath is given in relay mode.
    assert '"\\\\wsl.localhost\\Ubuntu\\home\\wunsch\\.voicemode\\logs\\ptt-listener.log"' in TEXT
    # The relay speaks {"action": ...} lines over TCP, as patches/ptt_relay.py reads them.
    assert "('{\"action\": \"' + $Action + '\"}' + \"`n\")" in TEXT
    assert "New-Object System.Net.Sockets.TcpClient" in TEXT


def test_native_mode_has_its_own_parameters():
    for name in ("SenderExe", "SenderArgs", "LogPath"):
        assert re.search(r"\[string\]\$" + name + r"\b", TEXT), name
    assert re.search(r"\[string\[\]\]\$SendAction\b", TEXT)
    assert '[string]$SenderArgs = "-m voice_mode.ptt_control_client --stdin"' in TEXT


def test_native_mode_is_chosen_by_the_sender():
    assert re.search(r"\$Native\s*=\s*\[bool\]\$SenderExe\b", TEXT)


def test_the_mapping_and_the_pipe_stay_in_python():
    # One mapping (ptt_control_client.COMMANDS) and one pipe client with the owner check
    # (control_transport). Pipe and token code next to the key polling was quarantined by
    # Windows Defender as malicious on 2026-11-07: it must not come back into this script.
    code = re.sub(r"<#.*?#>", "", TEXT, flags=re.S)
    code = "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))
    for needle in ('"skip_forward"', '"hold_end"', "NamedPipeClientStream", "CreateFileW", "OpenProcessToken",
                   "GetTokenInformation", "advapi32", "\\\\.\\pipe"):
        assert needle not in code, needle


def test_native_mode_drops_the_press_probe():
    probe = TEXT.index('Send-Action "press"')
    guard = TEXT.rindex("if (-not $Native)", 0, probe)
    assert TEXT.count("{", guard, probe) - TEXT.count("}", guard, probe) == 1, "the probe is not inside the guard"


def test_the_sender_is_told_the_outcome_words_the_client_answers():
    client = (REPO / "patches" / "ptt_control_client.py").read_text(encoding="utf-8")
    for word in ("sent", "none", "no-channel", "busy"):
        assert f'"{word}"' in TEXT and f'"{word}"' in client, word


def _ps(*args, timeout=60):
    script = subprocess.run(["wslpath", "-w", str(PS1)], capture_output=True, text=True).stdout.strip()
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, *args],
                          capture_output=True, text=True, timeout=timeout)


def _win(path):
    return subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True).stdout.strip()


@needs_powershell
@pytest.mark.parametrize("extra", [["-SenderExe", "python.exe", "-RelayHost", "127.0.0.1"],
                                   ["-SenderExe", "python.exe", "-Port", "8765"],
                                   ["-SenderArgs", "-m x"]])
def test_native_and_relay_parameters_do_not_mix(extra):
    done = _ps(*extra, "-SendAction", "short_press")
    assert done.returncode == 2, done.stdout + done.stderr
    assert "-SenderExe" in done.stdout


@needs_powershell
def test_a_sender_that_cannot_start_is_reported_not_thrown(tmp_path):
    log = tmp_path / "ptt.log"
    done = _ps("-SenderExe", r"C:\no\such\python.exe", "-LogPath", _win(log),
               "-SendAction", "short_press,hold_start")
    assert done.returncode == 0 and done.stderr == "", done.stdout + done.stderr
    text = log.read_text(encoding="utf-8-sig")
    assert text.count("control pipe failed:") == 1, text  # logged once, not per key
    assert "short_press sent=False" in text and "hold_start sent=False" in text


# --- the key rule (Robert, 2026-11-07): down opens the mic, a release within 0.8 s stops ---

def _code():
    code = re.sub(r"<#.*?#>", "", TEXT, flags=re.S)
    return "\n".join(line for line in code.splitlines() if not line.lstrip().startswith("#"))


def test_tap_max_is_one_named_constant_of_800_ms():
    assert re.search(r"\[int\]\$TapMaxMs = 800\b", TEXT)
    assert "-ge $TapMaxMs" in _code()


def test_key_down_sends_hold_start_without_waiting():
    code = _code()
    down = code.index("if ($isDown -and -not $down)")
    up = code.index("elseif (-not $isDown -and $down)")
    assert 'Send-Action "hold_start"' in code[down:up]
    assert "$HoldThresholdMs" not in code[down:]  # no threshold in the key loop


def test_release_maps_through_one_function():
    code = _code()
    fn = code[code.index("function Get-ReleaseAction"):]
    fn = fn[:fn.index("\n}") + 2]
    assert '"hold_release"' in fn and '"short_press"' in fn and "-ge $TapMaxMs" in fn
    up = code[code.index("elseif (-not $isDown -and $down)"):]
    assert "Get-ReleaseAction" in up[:up.index("\n        }")]


def test_native_mode_warms_the_sender_before_the_first_key():
    code = _code()
    loop = code.index("while ($true)")
    warm = code.index('Invoke-Sender "warm"')
    assert code.index("Start-Sender", code.index("if ($Native) {\n    try {")) < warm < loop
    client = (REPO / "patches" / "ptt_control_client.py").read_text(encoding="utf-8")
    assert '"warm"' in client and '"ready"' in client


@needs_powershell
@pytest.mark.parametrize("keys,expected", [("0:down,500:up", ["hold_start", "short_press"]),
                                           ("0:down,1200:up", ["hold_start", "hold_release"]),
                                           ("0:down,800:up", ["hold_start", "hold_release"])])
def test_replayed_keys_map_like_every_other_listener(tmp_path, keys, expected):
    log = tmp_path / "ptt.log"
    done = _ps("-Port", "9", "-LogPath", _win(log), "-ReplayKeys", keys)
    assert done.returncode == 0, done.stdout + done.stderr
    sent = re.findall(r"replay (\w+) sent=", log.read_text(encoding="utf-8-sig"))
    assert sent == expected
