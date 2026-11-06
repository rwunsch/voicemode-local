<#
.SYNOPSIS
    Set up VoiceMode local voice services for Claude Code on Windows.

.DESCRIPTION
    Three install modes:
      wsl-mcp         Windows MCP entry invokes voice-mode INSIDE WSL via wsl.exe.
                      Audio routes through WSLg. Proxies + Docker shared.
                      Recommended while voice-mode 8.6.1 has unresolved
                      Windows-native audio bugs (see docs/windows-issues.md).
      wsl-shared      voice-mode runs on Windows, proxies run in WSL (forwarded
                      via localhost). Currently affected by issue #5 (recording
                      loop hangs on Windows audio path).
      windows-native  Everything on Windows, no WSL, no Docker, no admin rights:
                      scripts\windows\voicemode_native.py installs a private
                      runtime and is the MCP command (docs/windows-native).
                      Runs on Windows PowerShell 5.1 and PowerShell 7.

    In the two WSL modes, Docker containers (voicemode-whisper, voicemode-kokoro,
    voicemode-piper) are managed via Docker Desktop and shared, and PowerShell 7
    is needed.

.PARAMETER Mode
    wsl-shared | windows-native. Prompts if not specified.

.PARAMETER OpenAIKey
    OpenAI API key. If not given, reuses any existing key in ~/.claude.json.

.PARAMETER NoPiper
    Skip Piper TTS (German/Dutch/etc.). Default: Piper enabled.

.PARAMETER NoStart
    Don't start services after install. Default: services started.

.PARAMETER WslPath
    WSL filesystem path to a working voicemode-local checkout. Used only for
    wsl-shared mode detection. Default: /home/<wsl-user>/git/voicemode-local

.PARAMETER Root
    windows-native: the install folder (VOICEMODE_NATIVE_ROOT). Default
    %LOCALAPPDATA%\voicemode-local-native. Given, the MCP entry carries it.

.PARAMETER InstallUv
    windows-native: install uv for this user if it is missing. Without it the
    exact command is printed and nothing is installed.

.PARAMETER Pill
    windows-native: switch the voice pill on after setup. Default off.

.PARAMETER Barge
    windows-native: switch interrupt by voice on after setup. Default off.

.PARAMETER Force
    windows-native: replace an existing, different voicemode MCP entry.

.PARAMETER AllowWslCheckout
    windows-native: install from a \\wsl path anyway. The MCP command would then
    need WSL running.

.PARAMETER Uninstall
    windows-native: stop the services and remove the MCP entry if it points at
    this checkout. The install folder stays unless -RemoveData is given.

.PARAMETER RemoveData
    With -Uninstall: also delete the install folder.

.PARAMETER ComputeMode
    auto | cpu | gpu | hybrid. Which Docker compute mode for the STT/TTS backends.
    auto (default) picks hybrid (Kokoro GPU + Whisper CPU) when an NVIDIA GPU is
    usable by Docker, otherwise cpu. gpu = both on CUDA. Uses the shared
    docker-compose.{gpu,hybrid}.yml overrides — identical to Linux/WSL.
#>
[CmdletBinding()]
param(
    [ValidateSet("wsl-mcp","wsl-shared","windows-native","")]
    [string]$Mode = "",
    [string]$OpenAIKey = "",
    [switch]$NoPiper,
    [switch]$NoStart,
    [string]$WslPath = "",
    # Compute mode for the Docker STT/TTS backends. "auto" -> hybrid if an NVIDIA
    # GPU is usable by Docker, else cpu. hybrid = Kokoro GPU + Whisper CPU (recommended).
    [ValidateSet("auto","cpu","gpu","hybrid")]
    [string]$ComputeMode = "auto",
    # windows-native only (see docs/windows-native/README.md)
    [string]$Root = "",
    [switch]$InstallUv,
    [switch]$Pill,
    [switch]$Barge,
    [switch]$Force,
    [switch]$AllowWslCheckout,
    [switch]$Uninstall,
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnablePiper = -not $NoPiper

function ok    { param([string]$m) Write-Host "  [+] $m" -ForegroundColor Green }
function fail  { param([string]$m) Write-Host "  [X] $m" -ForegroundColor Red }
function warn  { param([string]$m) Write-Host "  [!] $m" -ForegroundColor Yellow }
function header { param([string]$m) Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Cyan }

function Test-GpuAvailable {
    # True only if an NVIDIA GPU is usable BY DOCKER: the driver is visible
    # (nvidia-smi) AND Docker has the nvidia runtime registered. Mirrors the
    # Linux lib/compute.sh vm_detect_gpu check so all OSes agree.
    try { & nvidia-smi -L 2>$null | Out-Null; if ($LASTEXITCODE -ne 0) { return $false } }
    catch { return $false }
    try {
        $runtimes = docker info --format '{{json .Runtimes}}' 2>$null
        return ($runtimes -match '"nvidia"')
    } catch { return $false }
}

# Run a Python script via py -3.12 with embedded source. Avoids quoting hell.
function Invoke-Py {
    param([Parameter(Mandatory)][string]$Source)
    $tmp = New-TemporaryFile
    try {
        Set-Content -Path $tmp.FullName -Value $Source -Encoding UTF8
        & py -3.12 $tmp.FullName
        if ($LASTEXITCODE -ne 0) { throw "Python script exited $LASTEXITCODE" }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

# --- windows-native: no WSL, no Docker, no admin ------------------------------
# Written for Windows PowerShell 5.1 as well as 7: no ??, no ternary, no && or ||,
# two-argument Join-Path only. ASCII only below this line.

$UvInstallCommand = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'

# Reads and writes the voicemode entry of ~/.claude.json, keeping everything else.
$McpHelper = @'
import json, os, shutil, sys, tempfile, time
action, path = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8-sig") as f:  # a BOM is allowed
        data = json.load(f)
except FileNotFoundError:
    data = {}
entry = (data.get("mcpServers") or {}).get("voicemode")

def save():
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak." + time.strftime("%Y%m%d-%H%M%S"))
    fd, tmp = tempfile.mkstemp(prefix=".claude.json.", dir=os.path.dirname(os.path.abspath(path)))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def norm(p):
    return os.path.normcase(os.path.normpath(str(p)))

# state <path> <python> <script> <root> <default root> <1|0: the entry carries the root>
# ours: this script and this root (an entry without VOICEMODE_NATIVE_ROOT is the default root's);
# same: ours, and exactly what install would write. Anything else is another install's.
if action == "state":
    python, script, root, default, with_env = sys.argv[3:8]
    env = {"VOICEMODE_NATIVE_ROOT": root} if with_env == "1" else {}
    if not isinstance(entry, dict):
        state = "absent"
    else:
        args = [a for a in entry.get("args") or [] if isinstance(a, str)]
        entry_env = entry.get("env") or {}
        entry_root = entry_env.get("VOICEMODE_NATIVE_ROOT") or default
        ours = any(norm(a) == norm(script) for a in args) and norm(entry_root) == norm(root)
        same = (ours and norm(entry.get("command", "")) == norm(python)
                and [norm(a) for a in args] == [norm(script), "launch"]
                and {k: norm(v) for k, v in entry_env.items()} == {k: norm(v) for k, v in env.items()})
        state = "same" if same else "ours" if ours else "other"
    print(state)
    print(json.dumps(entry, ensure_ascii=False))
elif action == "set":
    python, script, root, with_env = sys.argv[3:7]
    env = {"VOICEMODE_NATIVE_ROOT": root} if with_env == "1" else {}
    data.setdefault("mcpServers", {})["voicemode"] = {
        "type": "stdio", "command": python, "args": [script, "launch"], "env": env}
    save()
elif action == "remove":
    if isinstance(entry, dict):
        data["mcpServers"].pop("voicemode")
        save()
'@

function Invoke-Helper {
    param([string]$Python, [string[]]$Arguments)
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("vml-mcp-" + [guid]::NewGuid().ToString("N") + ".py")
    try {
        [IO.File]::WriteAllText($tmp, $McpHelper, (New-Object Text.UTF8Encoding($false)))
        $out = & $Python $tmp @Arguments
        if ($LASTEXITCODE -ne 0) { throw "the MCP entry helper exited $LASTEXITCODE" }
        return $out
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
}

function Find-NativePython {
    # A Python 3.10+ to run the launcher: py -3, then python. Each is asked for its own path.
    $probe = "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')"
    $tries = @()
    if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) { $tries += ,@("py", "-3") }
    if (Get-Command python -CommandType Application -ErrorAction SilentlyContinue) { $tries += ,@("python") }
    foreach ($try in $tries) {
        $exe = $try[0]
        $rest = @($try | Select-Object -Skip 1)
        $out = & $exe @rest -c $probe 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ([string]($out | Select-Object -Last 1)).Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    }
    return $null
}

function Find-Uv {
    # The same override setup honours.
    if ($env:VOICEMODE_UV -and (Test-Path -LiteralPath $env:VOICEMODE_UV)) { return $env:VOICEMODE_UV }
    $found = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Path }
    # Where uv's own installer puts it for this user; a new shell has it on PATH.
    foreach ($candidate in @((Join-Path $env:USERPROFILE ".local\bin\uv.exe"), (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe"))) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Get-ClaudeCli {
    $found = Get-Command claude -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Path }
    return $null
}

function Get-NativeInfo {
    param([string]$Python, [string]$Script)
    $out = & $Python $Script info
    if ($LASTEXITCODE -ne 0) { return $null }
    return (($out | Select-Object -Last 1) | ConvertFrom-Json)
}

function Invoke-NativeInstall {
    $ErrorActionPreference = "Continue"  # native stderr must never stop us under 5.1
    $script = Join-Path $ScriptDir "scripts\windows\voicemode_native.py"
    header "windows-native: checks"
    if ($ScriptDir -like '\\wsl*' -and -not $AllowWslCheckout) {
        fail "This checkout is on a WSL path: $ScriptDir"
        Write-Host "      The MCP command would run the launcher from there, so voice would work only while"
        Write-Host "      WSL is running. Clone the repo to a Windows folder and run install.ps1 from there,"
        Write-Host "      or pass -AllowWslCheckout to install from here anyway."
        return 1
    }
    if (-not (Test-Path -LiteralPath $script)) { fail "Missing $script"; return 1 }
    $python = Find-NativePython
    if (-not $python) {
        fail "No Python 3.10 or newer found (tried py -3, then python)."
        Write-Host "      Install Python from https://www.python.org/downloads/ (no admin needed), then re-run."
        return 1
    }
    ok "Python: $python"
    $uv = Find-Uv
    if (-not $uv) {
        if (-not $InstallUv) {
            fail "uv not found. Install it for this user (no admin) with:"
            Write-Host "      $UvInstallCommand"
            Write-Host "      then re-run install.ps1, or re-run with -InstallUv to have it run that command."
            return 1
        }
        Write-Host "  Installing uv for this user: $UvInstallCommand"
        & powershell.exe -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Host
        $uv = Find-Uv
        if (-not $uv) { fail "uv is still not found after its installer ran."; return 1 }
    }
    ok "uv: $uv"
    $env:VOICEMODE_UV = $uv
    if ($Root) { $env:VOICEMODE_NATIVE_ROOT = $Root }

    header "windows-native: setup"
    & $python $script setup | Out-Host
    if ($LASTEXITCODE -ne 0) { fail "setup failed (exit $LASTEXITCODE); see the lines above."; return 1 }
    $info = Get-NativeInfo $python $script
    if (-not $info) { fail "Could not read the install's details (voicemode_native.py info)."; return 1 }
    # uv's minor-version link to its Python under the root survives runtime rebuilds; the
    # runtime's own venv Python does not. Else the Python found above.
    if ($info.python) {
        $mcpPython = $info.python
        ok "MCP Python: $mcpPython (uv's Python under the install folder; kept across rebuilds)"
    } else {
        $mcpPython = $python
        warn "MCP Python: $mcpPython (uv's Python link under the install folder was not found)"
    }

    header "windows-native: Claude Code MCP entry"
    $claudeJson = Join-Path $env:USERPROFILE ".claude.json"
    $withEnv = "0"
    if ($env:VOICEMODE_NATIVE_ROOT) { $withEnv = "1" }
    $state = Invoke-Helper $python @("state", $claudeJson, $mcpPython, $script, $info.root, $info.default_root, $withEnv)
    $kind = [string]($state | Select-Object -First 1)
    $existing = [string]($state | Select-Object -Last 1)
    $mcpLine = "voicemode -> `"$mcpPython`" `"$script`" launch"
    $claude = Get-ClaudeCli
    if ($kind -eq "same") {
        ok "voicemode is already registered: $mcpLine"
    } elseif ($kind -ne "absent" -and -not $Force) {
        warn "A voicemode MCP entry already exists and was not changed:"
        Write-Host "      $existing"
        Write-Host "      To replace it with this install's, re-run with -Force."
        $mcpLine = "unchanged (see above)"
    } else {
        if ($claude) {
            if ($kind -ne "absent") {
                & $claude mcp remove --scope user voicemode | Out-Host
                if ($LASTEXITCODE -ne 0) { fail "claude mcp remove failed (exit $LASTEXITCODE); the old entry is still there."; return 1 }
            }
            $addArgs = @("mcp", "add", "--scope", "user", "voicemode")
            if ($withEnv -eq "1") { $addArgs += @("-e", "VOICEMODE_NATIVE_ROOT=$($info.root)") }
            $addArgs += @("--", $mcpPython, $script, "launch")
            & $claude @addArgs | Out-Host
            if ($LASTEXITCODE -ne 0) { fail "claude mcp add failed (exit $LASTEXITCODE)"; return 1 }
            ok "Registered with: claude $($addArgs -join ' ')"
        } else {
            Invoke-Helper $python @("set", $claudeJson, $mcpPython, $script, $info.root, $withEnv) | Out-Null
            ok "claude CLI not found; wrote mcpServers.voicemode in $claudeJson (a backup is beside it)"
        }
    }

    foreach ($pair in @(@("pill", $Pill), @("barge", $Barge))) {
        if ($pair[1]) {
            & $mcpPython $script $pair[0] on | Out-Host
            if ($LASTEXITCODE -ne 0) { fail "$($pair[0]) on failed (exit $LASTEXITCODE)"; return 1 }
        }
    }

    header "Installed (windows-native)"
    Write-Host "  Root:      $($info.root)"
    Write-Host "  Runtime:   $($info.runtime)"
    & $mcpPython $script status | ForEach-Object { Write-Host "             $_" }
    Write-Host "  MCP entry: $mcpLine"
    Write-Host "  Pill:      & `"$mcpPython`" `"$script`" pill on|off|status"
    Write-Host "  Barge:     & `"$mcpPython`" `"$script`" barge on|off|status"
    Write-Host "  Uninstall: powershell -ExecutionPolicy Bypass -File `"$(Join-Path $ScriptDir 'install.ps1')`" -Mode windows-native -Uninstall [-RemoveData]"
    Write-Host "  Restart Claude Code (or run /mcp and reconnect voicemode) to use it."
    return 0
}

function Invoke-NativeUninstall {
    $ErrorActionPreference = "Continue"
    $script = Join-Path $ScriptDir "scripts\windows\voicemode_native.py"
    header "windows-native: uninstall"
    $python = Find-NativePython
    if (-not $python) { fail "No Python 3.10 or newer found (tried py -3, then python)."; return 1 }
    if ($Root) { $env:VOICEMODE_NATIVE_ROOT = $Root }
    $info = Get-NativeInfo $python $script
    if (-not $info) { fail "Could not find the install folder (voicemode_native.py info)."; return 1 }

    $claudeJson = Join-Path $env:USERPROFILE ".claude.json"
    # Matched by script AND root: another install from this checkout keeps its entry.
    $state = Invoke-Helper $python @("state", $claudeJson, $python, $script, $info.root, $info.default_root, "0")
    $kind = [string]($state | Select-Object -First 1)
    if ($kind -eq "same" -or $kind -eq "ours") {
        $claude = Get-ClaudeCli
        if ($claude) {
            & $claude mcp remove --scope user voicemode | Out-Host
            if ($LASTEXITCODE -ne 0) { fail "claude mcp remove failed (exit $LASTEXITCODE); the entry is still there."; return 1 }
        } else {
            Invoke-Helper $python @("remove", $claudeJson) | Out-Null
        }
        ok "Removed the voicemode MCP entry"
    } elseif ($kind -eq "other") {
        warn "The voicemode MCP entry is not this install's ($script, $($info.root)); left alone:"
        Write-Host "      $([string]($state | Select-Object -Last 1))"
    } else {
        ok "No voicemode MCP entry"
    }

    & $python $script stop | Out-Host
    if (-not $RemoveData) {
        ok "Kept the install folder $($info.root) (re-run with -RemoveData to delete it)"
        return 0
    }
    # The checks (never the WSL pill's or FELIX's folders, the profile or LOCALAPPDATA, nor a
    # folder holding one of them) live in voicemode_native.py remove-data, tested on Linux.
    & $python $script remove-data | Out-Host
    if ($LASTEXITCODE -ne 0) { fail "The install folder was not deleted (see above)."; return 1 }
    return 0
}

if ($Uninstall -and $Mode -ne "windows-native") {
    fail "-Uninstall is for -Mode windows-native only."
    exit 2
}
if ($Mode -eq "windows-native") {
    if ($Uninstall) { exit (Invoke-NativeUninstall) }
    exit (Invoke-NativeInstall)
}

# ─── Step 0: Mode selection ─────────────────────────────────────
header "Install mode"

if (-not $WslPath) {
    try {
        $wslUser = (wsl.exe -e bash -c "echo `$USER" 2>$null) -replace '\s',''
        if ($wslUser) { $WslPath = "/home/$wslUser/git/voicemode-local" }
    } catch {}
}

$wslAvailable = $false
if ($WslPath) {
    try {
        $check = wsl.exe -e bash -c "[ -f $WslPath/install.sh ] && echo YES || echo NO" 2>$null
        if ($check -match "YES") { $wslAvailable = $true }
    } catch {}
}

if (-not $Mode) {
    if ($wslAvailable) {
        Write-Host "  Detected WSL voicemode-local at $WslPath"
        Write-Host ""
        Write-Host "    1) wsl-mcp (recommended)    - voice-mode runs in WSL via wsl.exe"
        Write-Host "                                  (avoids open Windows audio bugs)"
        Write-Host "    2) wsl-shared               - voice-mode on Windows, proxies in WSL"
        Write-Host "    3) windows-native           - everything on Windows"
        Write-Host ""
        $c = Read-Host "  Choice [1]"
        $Mode = switch ($c) {
            "2" { "wsl-shared" }
            "3" { "windows-native" }
            default { "wsl-mcp" }
        }
    } else {
        $Mode = "windows-native"
        ok "WSL voicemode-local not detected; using windows-native mode"
    }
}
ok "Mode: $Mode"
if ($Mode -eq "windows-native") { exit (Invoke-NativeInstall) }
if ($PSVersionTable.PSVersion.Major -lt 7) {
    fail "The $Mode mode needs PowerShell 7 (pwsh). windows-native also runs on Windows PowerShell."
    exit 1
}

# ─── Step 1: Prerequisites ──────────────────────────────────────
header "Step 1: Prerequisites"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    fail "Python launcher 'py' not found. Install Python 3.10+ from python.org"
    exit 1
}
$pyVer = & py -3.12 --version 2>&1
if ($LASTEXITCODE -ne 0) { fail "Python 3.12 not found via 'py -3.12'"; exit 1 }
ok "Python: $pyVer"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    fail "uv not found. Install: irm https://astral.sh/uv/install.ps1 | iex"
    exit 1
}
ok "uv: $((uv --version) -split ' ' | Select-Object -Index 1)"

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    warn "claude CLI not found in PATH (Claude Code MCP registration will write directly to ~/.claude.json)"
} else {
    ok "claude CLI present"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    fail "Docker not found. Install Docker Desktop."
    exit 1
}
$dockerVersion = $null
try { $dockerVersion = (docker version --format '{{.Server.Version}}' 2>$null) } catch {}
if (-not $dockerVersion) { fail "Docker daemon not reachable. Start Docker Desktop."; exit 1 }
ok "Docker engine: $dockerVersion"

# ─── Skip Windows-side setup in wsl-mcp mode ────────────────────
if ($Mode -eq "wsl-mcp") {
    header "wsl-mcp mode: skipping Windows venv / patches / ffmpeg"
    ok "voice-mode will run inside WSL via wsl.exe; Windows side only registers the MCP entry"
    $skipWindows = $true
} else {
    $skipWindows = $false
}

# ─── Step 2: ffmpeg ─────────────────────────────────────────────
if (-not $skipWindows) {
header "Step 2: ffmpeg"
$ffmpegLink = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\ffmpeg.exe"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    ok "ffmpeg already installed: $((ffmpeg -version 2>&1 | Select-Object -First 1))"
} elseif (Test-Path $ffmpegLink) {
    ok "ffmpeg found at $ffmpegLink (open new shell to update PATH)"
    $env:Path = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;" + $env:Path
} else {
    Write-Host "  Installing ffmpeg via winget..."
    winget install --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements | Out-Null
    if (Test-Path $ffmpegLink) {
        ok "ffmpeg installed (restart shell to refresh PATH)"
        $env:Path = "$env:LOCALAPPDATA\Microsoft\WinGet\Links;" + $env:Path
    } else {
        warn "winget install did not produce expected ffmpeg link; install manually from ffmpeg.org"
    }
}

# ─── Step 3: voice-mode venv ────────────────────────────────────
header "Step 3: voice-mode in venv"
$venv = Join-Path $ScriptDir ".venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "  Creating venv (Python 3.12)..."
    $pyExe = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
    uv venv $venv --python $pyExe | Out-Null
}
$pkgs = @("voice-mode==8.12.0")  # pinned: patch anchors target this version
if ($Mode -eq "windows-native" -and $EnablePiper) { $pkgs += "piper-tts" }
Write-Host "  Installing: $($pkgs -join ', ')..."
uv pip install --python $venvPy @pkgs 2>&1 | Out-Null
ok "voice-mode installed in $venv"

# ─── Step 4: Apply patches ──────────────────────────────────────
header "Step 4: Patches"
$vmDir = Join-Path $venv "Lib\site-packages\voice_mode"
$patchSrc = Join-Path $ScriptDir "patches"

$patches = @(
    @{ Src = "conch.py";              Dst = (Join-Path $vmDir "conch.py") }
    @{ Src = "converse.py";           Dst = (Join-Path $vmDir "prompts\converse.py") }
    @{ Src = "switch_mode_prompt.py"; Dst = (Join-Path $vmDir "prompts\switch_mode.py") }
    @{ Src = "switch_mode.py";        Dst = (Join-Path $vmDir "tools\switch_mode.py") }
)
foreach ($p in $patches) {
    $src = Join-Path $patchSrc $p.Src
    if (Test-Path $src) {
        Copy-Item $src $p.Dst -Force
        ok "applied $($p.Src)"
    }
}

$initFile = Join-Path $vmDir "tools\__init__.py"
$initContent = Get-Content $initFile -Raw
if ($initContent -notmatch '"switch_mode"') {
    $newInit = $initContent -replace `
        'default_tools = \{"converse", "service", "connect_status"\}', `
        'default_tools = {"converse", "service", "connect_status", "switch_mode"}'
    Set-Content $initFile $newInit -NoNewline
    ok "tools/__init__.py default_tools updated"
} else {
    ok "tools/__init__.py already includes switch_mode"
}

} # end if -not $skipWindows (closes the block opened before Step 2)

# ─── Step 5: Register MCP server ────────────────────────────────
header "Step 5: Claude MCP registration"
$claudeJson = "$env:USERPROFILE\.claude.json"
if (-not (Test-Path $claudeJson)) { '{"mcpServers":{}}' | Set-Content $claudeJson -Encoding UTF8 }
$backup = "$claudeJson.bak.$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item $claudeJson $backup -Force
ok "Backup: $backup"

if ($Mode -eq "wsl-mcp") {
    # Windows-side MCP entry that invokes the voicemode-mcp WRAPPER inside WSL.
    # The wrapper auto-starts the needed proxies (detached) then exec's voice-mode.
    # Routing config is read from the WSL ~/.voicemode/voicemode.env (the same file
    # native WSL sessions use), so only OPENAI_API_KEY is passed across via WSLENV.
    $wslMcp = "$WslPath/voicemode-mcp"
    $pyScript = @"
import json, os, pathlib
p = pathlib.Path(r"$claudeJson")
with p.open(encoding='utf-8') as f: d = json.load(f)
mcp = d.setdefault('mcpServers', {})
existing_env = mcp.get('voicemode', {}).get('env', {}) or {}
key = r"$OpenAIKey".strip() or existing_env.get('OPENAI_API_KEY', '')
mcp['voicemode'] = {
    'type': 'stdio',
    'command': 'wsl.exe',
    'args': ['-d', 'Ubuntu', '-e', 'bash', '-c', '$wslMcp'],
    'env': {
        'OPENAI_API_KEY': key,
        'WSLENV': 'OPENAI_API_KEY/u',
    },
}
with p.open('w', encoding='utf-8') as f: json.dump(d, f, indent=2)
print('voicemode MCP registered (wsl-mcp: runs voicemode-mcp wrapper in WSL; routing from voicemode.env)')
"@
    Invoke-Py -Source $pyScript
    return  # Skip the rest — Windows-side install steps don't apply
}

$voiceModeBin = Join-Path $venv "Scripts\voice-mode.exe"

# Build PATH that includes the winget links dir so voice-mode's shutil.which('ffmpeg')
# resolves regardless of when the parent shell was started. We snapshot the
# current PATH as the base — the spawned MCP server gets exactly this PATH,
# replacing whatever it would inherit. (.cmd wrapper approach didn't work:
# Claude Code's MCP loader on Windows treats .cmd inconsistently.)
$wingetLinks = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links"
$mcpPath = "$wingetLinks;" + $env:Path

# Routing config (STT/TTS endpoints, voices) is read from voicemode.env at
# startup — see Configuration in the README. Only the secret + PATH go here.
$pyScript = @"
import json, os, pathlib
p = pathlib.Path(r"$claudeJson")
with p.open(encoding='utf-8') as f: d = json.load(f)
mcp = d.setdefault('mcpServers', {})
existing_env = mcp.get('voicemode', {}).get('env', {}) or {}
key = r"$OpenAIKey".strip() or existing_env.get('OPENAI_API_KEY', '')
mcp['voicemode'] = {
    'type': 'stdio',
    'command': r"$voiceModeBin",
    'args': [],
    'env': {
        'OPENAI_API_KEY': key,
        'PATH': r"$mcpPath",
    },
}
with p.open('w', encoding='utf-8') as f: json.dump(d, f, indent=2)
print('voicemode MCP registered (windows-native; routing from voicemode.env)')
"@
Invoke-Py -Source $pyScript

# ─── Step 6: Permissions ────────────────────────────────────────
header "Step 6: Claude permissions"
$settingsDir = Join-Path $env:USERPROFILE ".claude"
$settingsPath = Join-Path $settingsDir "settings.json"
if (-not (Test-Path $settingsDir)) { New-Item -ItemType Directory -Path $settingsDir | Out-Null }

$pyScript = @"
import json, os, pathlib
p = pathlib.Path(r"$settingsPath")
d = {}
if p.exists():
    with p.open(encoding='utf-8') as f: d = json.load(f)
allow = d.setdefault('permissions', {}).setdefault('allow', [])
for x in ['mcp__voicemode__converse', 'mcp__voicemode__service', 'mcp__voicemode__switch_mode']:
    if x not in allow: allow.append(x)
with p.open('w', encoding='utf-8') as f: json.dump(d, f, indent=2)
print('permissions updated')
"@
Invoke-Py -Source $pyScript

# ─── Step 7: Start services ─────────────────────────────────────
header "Step 7: Services"
if ($NoStart) {
    warn "NoStart specified — skipping service startup"
} else {
    Push-Location $ScriptDir
    try {
        # Resolve compute mode (auto -> hybrid if a GPU is usable by Docker, else cpu)
        # and stack the matching override. Same compose files as Linux/WSL.
        $resolvedCompute = $ComputeMode
        if ($resolvedCompute -eq "auto") {
            if (Test-GpuAvailable) { $resolvedCompute = "hybrid" } else { $resolvedCompute = "cpu" }
        }
        $composeArgs = @("-f","docker-compose.yml")
        switch ($resolvedCompute) {
            "gpu"    { $composeArgs += @("-f","docker-compose.gpu.yml");    $env:WHISPER_MODEL = "small" }
            "hybrid" { $composeArgs += @("-f","docker-compose.hybrid.yml"); $env:WHISPER_MODEL = "base"  }
            default  { $env:WHISPER_MODEL = "base" }
        }
        if ($EnablePiper) { $composeArgs += @("--profile","piper") }
        $composeArgs += @("up","-d")
        if ($resolvedCompute -ne "cpu") {
            Write-Host "  (CUDA image(s) for '$resolvedCompute' download on first start — can take several minutes)"
        }
        Write-Host "  Starting Docker containers (compute mode: $resolvedCompute)..."
        docker compose @composeArgs 2>&1 | ForEach-Object { Write-Host "    $_" }
    } finally {
        Pop-Location
    }

    if ($Mode -eq "windows-native") {
        $servicesScript = Join-Path $ScriptDir "voicemode-services.ps1"
        if (Test-Path $servicesScript) {
            Write-Host "  Starting Windows-native proxies..."
            & $servicesScript -Action start -EnablePiper:$EnablePiper
        } else {
            warn "voicemode-services.ps1 not found — start proxies manually"
        }
    } else {
        # wsl-shared: verify reachability
        Start-Sleep -Seconds 2
        foreach ($p in @(2022,8881)) {
            try {
                $r = Invoke-WebRequest "http://127.0.0.1:$p/health" -TimeoutSec 3 -UseBasicParsing
                ok "WSL proxy on port $p reachable (HTTP $($r.StatusCode))"
            } catch {
                warn "WSL proxy on port $p NOT reachable. In WSL run: cd $WslPath; ./voicemode-switch start"
            }
        }
    }
}

header "Installation complete"
Write-Host ""
Write-Host "  Restart Claude Code, then try /voicemode:converse"
Write-Host ""
if ($Mode -eq "windows-native") {
    Write-Host "  Manage Windows proxies with: .\voicemode-services.ps1 [start|stop|status]"
}
Write-Host "  Switch TTS modes with: voicemode-switch local|localonly|piper|openai|hybrid"
Write-Host "    (writes ~/.voicemode/voicemode.env; run it in WSL for the bridge)"
Write-Host ""
