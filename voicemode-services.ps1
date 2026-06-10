#requires -Version 7.0
<#
.SYNOPSIS
    Manage Windows-native VoiceMode proxies (whisper-proxy + piper-proxy).

.DESCRIPTION
    Used in windows-native install mode. The proxies are plain Python http.server
    scripts; this wrapper starts/stops them as background processes and tracks
    their PIDs in $env:TEMP. Logs go to $env:TEMP\voicemode-*.log.

    Not needed in wsl-shared mode — there the proxies live in WSL.

.PARAMETER Action
    start | stop | status | restart

.PARAMETER WhisperPort
    Port for whisper-proxy (default 2022). Use a different port if WSL has 2022 bound.

.PARAMETER PiperPort
    Port for piper-proxy (default 8881).

.PARAMETER WhisperUrl
    Upstream Whisper container URL (default http://127.0.0.1:9000).

.PARAMETER NoPiper
    Skip piper-proxy.
#>
[CmdletBinding()]
param(
    [Parameter(Position=0)]
    [ValidateSet("start","stop","status","restart")]
    [string]$Action = "status",
    [int]$WhisperPort = 2022,
    [int]$PiperPort = 8881,
    [string]$WhisperUrl = "http://127.0.0.1:9000",
    [switch]$NoPiper,
    [switch]$EnablePiper
)

# -EnablePiper is the explicit enable; -NoPiper disables. Default: enabled.
if ($NoPiper) { $piperOn = $false }
elseif ($EnablePiper) { $piperOn = $true }
else { $piperOn = $true }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv      = Join-Path $ScriptDir ".venv"
$Py        = Join-Path $Venv "Scripts\python.exe"
$Tmp       = $env:TEMP

$Services = @(
    @{
        Name    = "whisper-proxy"
        Script  = Join-Path $ScriptDir "whisper-proxy.py"
        ArgList = @("--port", $WhisperPort, "--whisper-url", $WhisperUrl)
        Port    = $WhisperPort
        Health  = "http://127.0.0.1:$WhisperPort/health"
        PidFile = Join-Path $Tmp "voicemode-whisper-proxy.pid"
        LogFile = Join-Path $Tmp "voicemode-whisper-proxy.log"
        Enabled = $true
    },
    @{
        Name    = "piper-proxy"
        Script  = Join-Path $ScriptDir "piper-proxy.py"
        ArgList = @(
            "--port", $PiperPort,
            "--voices-file", (Join-Path $ScriptDir "voices\piper-voices.json"),
            "--models-dir",  (Join-Path $ScriptDir "models\piper")
        )
        Port    = $PiperPort
        Health  = "http://127.0.0.1:$PiperPort/health"
        PidFile = Join-Path $Tmp "voicemode-piper-proxy.pid"
        LogFile = Join-Path $Tmp "voicemode-piper-proxy.log"
        Enabled = $piperOn
    }
)

function ok    { param([string]$m) Write-Host "  [+] $m" -ForegroundColor Green }
function fail  { param([string]$m) Write-Host "  [X] $m" -ForegroundColor Red }
function warn  { param([string]$m) Write-Host "  [!] $m" -ForegroundColor Yellow }

function Get-LiveProc {
    param([string]$PidFile)
    if (-not (Test-Path $PidFile)) { return $null }
    $procId = Get-Content $PidFile -ErrorAction SilentlyContinue
    if (-not $procId) { return $null }
    return Get-Process -Id $procId -ErrorAction SilentlyContinue
}

function Start-Svc {
    param($Svc)
    if (-not $Svc.Enabled) { warn "$($Svc.Name): disabled, skipping"; return }

    $existing = Get-LiveProc -PidFile $Svc.PidFile
    if ($existing) {
        warn "$($Svc.Name): already running (PID $($existing.Id))"
        return
    }

    # Detect port collision with wslrelay or anything else
    $owner = Get-NetTCPConnection -State Listen -LocalPort $Svc.Port -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($owner) {
        $ownerProc = Get-Process -Id $owner.OwningProcess -ErrorAction SilentlyContinue
        fail ("$($Svc.Name): port {0} already bound by {1} (PID {2}). " +
              "If that's wslrelay (i.e. WSL has the proxy), either use wsl-shared mode " +
              "or pick a different port via -WhisperPort / -PiperPort.") `
              -f $Svc.Port, $ownerProc.ProcessName, $owner.OwningProcess
        return
    }

    $allArgs = @($Svc.Script) + $Svc.ArgList
    $proc = Start-Process -FilePath $Py -ArgumentList $allArgs `
        -WorkingDirectory $ScriptDir `
        -RedirectStandardOutput $Svc.LogFile `
        -RedirectStandardError ("$($Svc.LogFile).err") `
        -WindowStyle Hidden -PassThru

    Set-Content -Path $Svc.PidFile -Value $proc.Id
    Start-Sleep -Seconds 1

    if ($proc.HasExited) {
        fail "$($Svc.Name): exited immediately. See $($Svc.LogFile).err"
        Remove-Item $Svc.PidFile -ErrorAction SilentlyContinue
    } else {
        ok "$($Svc.Name): started (PID $($proc.Id), port $($Svc.Port))"
    }
}

function Stop-Svc {
    param($Svc)
    $existing = Get-LiveProc -PidFile $Svc.PidFile
    if (-not $existing) {
        warn "$($Svc.Name): not running"
        Remove-Item $Svc.PidFile -ErrorAction SilentlyContinue
        return
    }
    Stop-Process -Id $existing.Id -Force -ErrorAction SilentlyContinue
    Remove-Item $Svc.PidFile -ErrorAction SilentlyContinue
    ok "$($Svc.Name): stopped (was PID $($existing.Id))"
}

function Status-Svc {
    param($Svc)
    $proc = Get-LiveProc -PidFile $Svc.PidFile
    $procStr = if ($proc) { "PID $($proc.Id)" } else { "not running" }
    $healthStr = "?"
    try {
        $r = Invoke-WebRequest $Svc.Health -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
        $healthStr = "HTTP $($r.StatusCode)"
    } catch {
        $healthStr = "unreachable"
    }
    "{0,-14} {1,-15} {2}" -f $Svc.Name, $procStr, "$($Svc.Health) -> $healthStr"
}

switch ($Action) {
    "start"   { foreach ($s in $Services) { Start-Svc $s } }
    "stop"    { foreach ($s in $Services) { Stop-Svc  $s } }
    "restart" { foreach ($s in $Services) { Stop-Svc $s }; Start-Sleep 1; foreach ($s in $Services) { Start-Svc $s } }
    "status"  { foreach ($s in $Services) { Status-Svc $s } }
}
