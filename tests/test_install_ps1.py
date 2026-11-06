"""Static and helper checks of install.ps1's windows-native mode, on Linux.

The real install runs on Windows: tests/test_voicemode_native_windows.py runs it under
powershell.exe 5.1 into a temp root with a fake claude CLI. Here: it must parse on
Windows PowerShell 5.1 (a UTF-8 BOM, no PowerShell 7 syntax in the native part), the
WSL modes' code must be byte-for-byte what it was, and the Python helper that edits
~/.claude.json must keep the file's other content. With powershell.exe reachable (WSL),
the refusal to install from a \\\\wsl path is run for real; it stops before any step.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.ps1"
NATIVE_START = "# --- windows-native: no WSL, no Docker, no admin"
WSL_START = "# \u2500\u2500\u2500 Step 0: Mode selection"
# The five lines added after `ok "Mode: $Mode"`: everything else from Step 0 on is the
# WSL modes' code as it was at 223edb7.
ADDED = ('if ($Mode -eq "windows-native") { exit (Invoke-NativeInstall) }\n'
         'if ($PSVersionTable.PSVersion.Major -lt 7) {\n'
         '    fail "The $Mode mode needs PowerShell 7 (pwsh). windows-native also runs on Windows PowerShell."\n'
         '    exit 1\n'
         '}\n')
WSL_TAIL_SHA256 = "b9430d5bcdcb739e4458f2665d93983fe6f66cdf3b8942728901abf8355c41a0"  # of the WSL modes' code at 223edb7, from Step 0 on


def _text():
    raw = INSTALL.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install.ps1 needs a UTF-8 BOM for Windows PowerShell 5.1"
    return raw[3:].decode("utf-8")


def _native():
    text = _text()
    return text[text.index(NATIVE_START):text.index(WSL_START)]


def test_no_powershell_7_requirement_for_the_whole_file():
    assert not re.search(r"(?im)^#requires\s+-version\s+7", _text())


def test_bom_lets_windows_powershell_read_the_non_ascii_comments():
    # Measured 2026-11-06: the file without a BOM, decoded as cp1252 (Windows PowerShell 5.1's
    # default on most machines), fails to parse with 7 errors; this machine's ACP is 65001.
    assert _text().startswith("<#")


def test_native_part_is_ascii_and_free_of_powershell_7_syntax():
    native = _native()
    assert native.isascii()
    code = [line for line in native.splitlines() if not line.lstrip().startswith("#")]
    helper = native[native.index("$McpHelper = @'"):native.index("'@")]
    ps = [line for line in code if line not in helper.splitlines()]
    for line in ps:
        assert "??" not in line, line
        assert "&&" not in line and "||" not in line, line
        assert not re.search(r"\s\?\s", line), line          # ternary
        arg = r"""(\$[\w.]+|"[^"]*"|'[^']*'|\([^()]*(?:\([^()]*\)[^()]*)*\))"""
        assert not re.search(rf"Join-Path\s+{arg}\s+{arg}\s+{arg}", line), line  # 3+ args: PS 6+
        assert "-AsHashtable" not in line and "?." not in line, line


def test_wsl_modes_are_unchanged():
    text = _text()
    tail = text[text.index(WSL_START):]
    assert tail.count(ADDED) == 1
    digest = hashlib.sha256(tail.replace(ADDED, "", 1).encode("utf-8")).hexdigest()
    original = subprocess.run(["git", "-C", str(REPO), "show", "223edb7:install.ps1"],
                              capture_output=True, check=False)
    if original.returncode == 0:
        old = original.stdout.decode("utf-8")
        assert tail.replace(ADDED, "", 1) == old[old.index(WSL_START):]
    assert digest == WSL_TAIL_SHA256


def test_windows_native_runs_before_any_wsl_or_docker_step():
    text = _text()
    native_exit = text.index('if ($Mode -eq "windows-native") {\n    if ($Uninstall)')
    assert native_exit < text.index('wsl.exe -e bash -c "echo') < text.index("docker version --format")
    assert text.index('if ($Mode -eq "windows-native") { exit (Invoke-NativeInstall) }') < text.index("Step 1: Prerequisites")


def test_mcp_registration_uses_the_claude_cli_form():
    native = _native()
    assert '@("mcp", "add", "--scope", "user", "voicemode")' in native
    assert '@("--", $mcpPython, $script, "launch")' in native
    assert "-Force" in native and "AllowWslCheckout" in native and "RemoveData" in native


# -- the ~/.claude.json helper ------------------------------------------------

@pytest.fixture
def helper(tmp_path):
    native = _native()
    body = native[native.index("$McpHelper = @'\n") + len("$McpHelper = @'\n"):native.index("\n'@")]
    path = tmp_path / "helper.py"
    path.write_text(body + "\n", encoding="utf-8")

    def run(*args):
        done = subprocess.run([sys.executable, str(path), *map(str, args)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout.splitlines()
    return run


PY = r"C:\r\python\cpython-3.12-windows-x86_64-none\python.exe"
SCRIPT = r"C:\git\vml\scripts\windows\voicemode_native.py"
DEFAULT = r"C:\Users\me\AppData\Local\voicemode-local-native"


def test_helper_set_keeps_other_content_and_state_reads_it(helper, tmp_path):
    claude = tmp_path / ".claude.json"
    claude.write_text(json.dumps({"projects": {"x": 1}, "mcpServers": {"other": {"command": "o"}}}))
    assert helper("state", claude, PY, SCRIPT, DEFAULT, DEFAULT, "0")[0] == "absent"
    helper("set", claude, PY, SCRIPT, DEFAULT, "0")
    data = json.loads(claude.read_text())
    assert data["projects"] == {"x": 1} and data["mcpServers"]["other"] == {"command": "o"}
    assert data["mcpServers"]["voicemode"] == {"type": "stdio", "command": PY, "args": [SCRIPT, "launch"], "env": {}}
    assert helper("state", claude, PY, SCRIPT, DEFAULT, DEFAULT, "0")[0] == "same"
    assert helper("state", claude, r"C:\other\python.exe", SCRIPT, DEFAULT, DEFAULT, "0")[0] == "ours"
    assert helper("state", claude, PY, SCRIPT, DEFAULT, DEFAULT, "1")[0] == "ours"   # should carry the root
    assert helper("state", claude, PY, r"C:\elsewhere\voicemode_native.py", DEFAULT, DEFAULT, "0")[0] == "other"
    assert len(list(tmp_path.glob(".claude.json.bak.*"))) == 1
    helper("remove", claude)
    assert "voicemode" not in json.loads(claude.read_text())["mcpServers"]


def test_helper_two_roots_from_one_checkout_are_told_apart(helper, tmp_path):
    claude = tmp_path / ".claude.json"
    second = r"C:\Users\me\vml-second"
    helper("set", claude, PY, SCRIPT, second, "1")
    entry = json.loads(claude.read_text())["mcpServers"]["voicemode"]
    assert entry["env"] == {"VOICEMODE_NATIVE_ROOT": second}
    assert helper("state", claude, PY, SCRIPT, second, DEFAULT, "1")[0] == "same"
    # The default install's uninstall must leave the second install's entry alone.
    assert helper("state", claude, PY, SCRIPT, DEFAULT, DEFAULT, "0")[0] == "other"
    helper("set", claude, PY, SCRIPT, DEFAULT, "0")
    assert helper("state", claude, PY, SCRIPT, second, DEFAULT, "1")[0] == "other"


def test_helper_reads_a_bom_and_keeps_non_ascii(helper, tmp_path):
    claude = tmp_path / ".claude.json"
    claude.write_bytes(b"\xef\xbb\xbf" + json.dumps({"projects": {"C:/J\u00fcrgen": 1}}, ensure_ascii=False).encode())
    helper("set", claude, PY, SCRIPT, DEFAULT, "0")
    text = claude.read_text(encoding="utf-8")
    assert "C:/J\u00fcrgen" in text and "\\u00fc" not in text


def test_helper_creates_the_file_when_missing(helper, tmp_path):
    claude = tmp_path / ".claude.json"
    helper("set", claude, "py", "s", r"C:\root", "1")
    assert json.loads(claude.read_text())["mcpServers"]["voicemode"]["env"] == {"VOICEMODE_NATIVE_ROOT": r"C:\root"}


def test_uninstall_matches_the_entry_by_root_and_checks_claude_exit_codes():
    native = _native()
    body = native[native.index("function Invoke-NativeUninstall"):]
    assert '@("state", $claudeJson, $python, $script, $info.root, $info.default_root' in body
    assert "remove-data" in body and "Remove-Item -LiteralPath $target" not in native
    for chunk in (body, native[native.index("function Invoke-NativeInstall"):native.index("function Invoke-NativeUninstall")]):
        after = chunk[chunk.index("mcp remove --scope user voicemode"):]
        assert after.index("$LASTEXITCODE") < 200, "claude mcp remove's exit code is not checked"


# -- for real, through powershell.exe ----------------------------------------

POWERSHELL = shutil.which("powershell.exe")


@pytest.mark.skipif(not POWERSHELL or not shutil.which("wslpath"), reason="needs WSL with powershell.exe")
def test_native_mode_refuses_a_wsl_checkout_under_windows_powershell():
    windows_path = subprocess.run(["wslpath", "-w", str(INSTALL)], capture_output=True, text=True).stdout.strip()
    assert windows_path.startswith("\\\\wsl")
    done = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", windows_path,
                           "-Mode", "windows-native"], capture_output=True, text=True, timeout=120)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "WSL path" in done.stdout and "-AllowWslCheckout" in done.stdout
    assert "Python:" not in done.stdout and "windows-native: setup" not in done.stdout
