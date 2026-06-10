#requires -Version 7.0
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
      windows-native  Everything on Windows: voice-mode, proxies, no WSL needed.
                      Also affected by issue #5 today.

    In all modes, Docker containers (voicemode-whisper, voicemode-kokoro,
    voicemode-piper) are managed via Docker Desktop and shared.

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
#>
[CmdletBinding()]
param(
    [ValidateSet("wsl-mcp","wsl-shared","windows-native","")]
    [string]$Mode = "",
    [string]$OpenAIKey = "",
    [switch]$NoPiper,
    [switch]$NoStart,
    [string]$WslPath = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnablePiper = -not $NoPiper

function ok    { param([string]$m) Write-Host "  [+] $m" -ForegroundColor Green }
function fail  { param([string]$m) Write-Host "  [X] $m" -ForegroundColor Red }
function warn  { param([string]$m) Write-Host "  [!] $m" -ForegroundColor Yellow }
function header { param([string]$m) Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Cyan }

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
$pkgs = @("voice-mode")
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
    # Windows-side MCP entry that invokes voice-mode INSIDE WSL.
    # WSLENV propagates the env vars from Windows into the WSL bash session
    # so they reach the spawned voice-mode process (/u = Unix-side only).
    $wslVoiceMode = "$WslPath/.venv/bin/voice-mode"
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
    'args': ['-d', 'Ubuntu', '-e', 'bash', '-c', '$wslVoiceMode'],
    'env': {
        'OPENAI_API_KEY': key,
        'STT_BASE_URL': 'http://127.0.0.1:2022/v1',
        'TTS_BASE_URL': 'http://127.0.0.1:8881/v1',
        'TTS_VOICE': 'p_de_thorsten',
        'WSLENV': 'OPENAI_API_KEY/u:STT_BASE_URL/u:TTS_BASE_URL/u:TTS_VOICE/u',
    },
}
with p.open('w', encoding='utf-8') as f: json.dump(d, f, indent=2)
print('voicemode MCP registered (wsl-mcp mode: invokes voice-mode in WSL via wsl.exe)')
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

# STT/TTS endpoints are the same in both modes — proxies always answer on
# 127.0.0.1:2022 and 127.0.0.1:8881 (whether they live in WSL or on Windows).
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
        'STT_BASE_URL': 'http://127.0.0.1:2022/v1',
        'TTS_BASE_URL': 'http://127.0.0.1:8881/v1',
        'TTS_VOICE': 'p_de_thorsten',
        'PATH': r"$mcpPath",
    },
}
with p.open('w', encoding='utf-8') as f: json.dump(d, f, indent=2)
print('voicemode MCP registered')
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
        $composeArgs = @()
        if ($EnablePiper) { $composeArgs += @("--profile","piper") }
        $composeArgs += @("up","-d")
        Write-Host "  Starting Docker containers..."
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
Write-Host "  Switch TTS modes by editing %USERPROFILE%\.claude.json mcpServers.voicemode.env"
Write-Host "    local  -> STT 2022, TTS http://127.0.0.1:8880/v1, voice af_sky"
Write-Host "    piper  -> STT 2022, TTS http://127.0.0.1:8881/v1, voice p_de_thorsten"
Write-Host "    openai -> remove STT_BASE_URL, TTS_BASE_URL, TTS_VOICE"
Write-Host ""
