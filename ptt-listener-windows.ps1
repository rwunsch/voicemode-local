<#
.SYNOPSIS
    Windows push-to-talk listener for VoiceMode Local: WSL2 relay or native.

.DESCRIPTION
    Watches for Ctrl+Space. Two modes, chosen by -SenderExe:

    Relay mode (no -SenderExe; WSL2, unchanged): forwards press / hold /
    release to the WSL-side relay (patches/ptt_relay.py), which hands them to
    voice-mode's control socket.

    Native mode (-SenderExe; voice-mode runs on Windows, no WSL): hands each
    action to a sender process, `python -m voice_mode.ptt_control_client
    --stdin`, one line in and one outcome line back. The sender maps it
    (a tap is skip_forward, a hold is hold_start then hold_end), names the
    control pipe from the same settings voice-mode reads, checks the pipe's
    owner and server before writing (patches/control_transport.py), and writes
    the control channel's JSON line. This script never opens the pipe itself:
    a version that did, with the token checks beside the key polling, was
    quarantined by Windows Defender as malicious (2026-11-07). No pipe means
    no voice turn is running: the key does nothing, and that is logged once.
    scripts/windows/voicemode_native.py starts it in this mode.

    Why PowerShell rather than the Python companion: a WSL terminal is a
    *Windows* window, so an X11 listener inside the guest never sees the key.
    Capture has to happen on the Windows side -- and PowerShell is already on
    every Windows box, where Python may not be.

    Why polling rather than a low-level keyboard hook: a hook needs a message
    pump and marshals every keystroke in the system through this process. A
    ~50ms poll of GetAsyncKeyState is far less invasive, sees only the two keys
    it asks about, and is plenty responsive for hold-to-talk.

.PARAMETER Port
    The WSL relay's port. Default 8765 (VOICEMODE_PTT_PORT on the WSL side).

.PARAMETER RelayHost
    Where the relay listens. Default 127.0.0.1, which works when the relay is
    bound to 0.0.0.0 inside WSL and reached through localhostForwarding.

.PARAMETER HoldThresholdMs
    Press shorter than this is a short press; longer becomes a hold.
    Default 400ms. (ptt_core uses 1000ms; 400 feels better with a real key.)

.PARAMETER SenderExe
    Native mode: the Python that runs the sender (the native runtime's).
    Cannot be combined with -Port or -RelayHost.

.PARAMETER SenderArgs
    Native mode: the sender's arguments, one Windows command line. Default
    "-m voice_mode.ptt_control_client --stdin".

.PARAMETER LogPath
    The log file. Default in relay mode: the WSL user's
    ~/.voicemode/logs/ptt-listener.log, as before. Native mode without it logs
    to the console only.

.PARAMETER SendAction
    Diagnosis and tests: send these actions (short_press, hold_start,
    hold_release, press; comma-separated or repeated) through the same send
    function the keys use, log each, then exit 0. Reads no keys.

.EXAMPLE
    .\ptt-listener-windows.ps1
    .\ptt-listener-windows.ps1 -HoldThresholdMs 300 -RelayHost 172.24.32.135
    .\ptt-listener-windows.ps1 -SenderExe <runtime>\.venv\Scripts\python.exe -SendAction short_press

.NOTES
    Ctrl+C to stop. Nothing is installed and nothing persists.
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$RelayHost = "127.0.0.1",
    [int]$HoldThresholdMs = 400,
    [string]$SenderExe = "",
    [string]$SenderArgs = "-m voice_mode.ptt_control_client --stdin",
    [string]$LogPath = "",
    [string[]]$SendAction = @()
)

$ErrorActionPreference = "Stop"

$Native = [bool]$SenderExe
if (($Native -and ($PSBoundParameters.ContainsKey("Port") -or $PSBoundParameters.ContainsKey("RelayHost"))) -or
    (-not $Native -and $PSBoundParameters.ContainsKey("SenderArgs"))) {
    Write-Host "  -SenderExe (native) cannot be combined with -Port or -RelayHost (the WSL relay), and -SenderArgs needs -SenderExe."
    exit 2
}

# Always log to a fixed file as well as the console. However this script gets
# launched -- Startup folder, Start-Process, a job, a plain window -- the log is
# readable from WSL, so a silent failure can never look identical to "no keys
# pressed" again.
# Native mode has no fixed file: the launcher passes -LogPath.
if ($LogPath) {
    $LogFile = $LogPath
} elseif ($Native) {
    $LogFile = ""
} else {
    $LogFile = "\\wsl.localhost\Ubuntu\home\wunsch\.voicemode\logs\ptt-listener.log"
}
function Log {
    param([string]$Text, [string]$Colour = "Gray")
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $Text
    Write-Host "  $line" -ForegroundColor $Colour
    if ($LogFile) { try { $line | Out-File -Append -FilePath $LogFile -Encoding utf8 } catch { } }
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class VmlKey {
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int vKey);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr hWnd);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    public static uint ForegroundPid() { uint pid; GetWindowThreadProcessId(GetForegroundWindow(), out pid); return pid; }
    public static bool Down(int vKey) { return (GetAsyncKeyState(vKey) & 0x8000) != 0; }
    public static string ForegroundTitle() {
        IntPtr h = GetForegroundWindow();
        int len = GetWindowTextLength(h);
        if (len <= 0) return "";
        var sb = new System.Text.StringBuilder(len + 1);
        GetWindowText(h, sb, sb.Capacity);
        return sb.ToString();
    }
}
"@

$VK_CONTROL = 0x11
$VK_SPACE   = 0x20

try {
    [void][VmlKey]::Down($VK_CONTROL)
    [void][VmlKey]::ForegroundPid()
} catch {
    Log ("FATAL: key interop unavailable -- " + $_.Exception.Message) "Red"
    exit 1
}

# Only fire while a terminal has focus, so Ctrl+Space in another app (where it
# is often "next input method") cannot grab the microphone.
#
# Match the foreground PROCESS, not the window title. Titles are whatever the
# app sets -- Claude Code puts the session name there, so this box shows
# "Voicemode", which no sane title pattern would have matched. Process names are
# stable.
$TerminalProcessPattern = 'WindowsTerminal|powershell|pwsh|wsl|conhost|cmd|alacritty|wezterm|Code'

function Test-TerminalFocused {
    try {
        $p = Get-Process -Id ([VmlKey]::ForegroundPid()) -ErrorAction Stop
        return $p.ProcessName -match $TerminalProcessPattern
    } catch { return $false }
}

function Send-RelayAction {
    param([string]$Action)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($RelayHost, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(500)) { $client.Close(); return $false }
        $client.EndConnect($iar)
        $stream = $client.GetStream()
        $bytes  = [System.Text.Encoding]::UTF8.GetBytes(('{"action": "' + $Action + '"}' + "`n"))
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $client.Close()
        return $true
    } catch {
        # Relay down or restarting -- PTT is simply inert, never noisy.
        return $false
    }
}

# --- native mode: the sender ------------------------------------------------
$script:Sender = $null
$script:SenderStarts = 0
$script:Outcome = ""

function Start-Sender {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $SenderExe
    $info.Arguments = $SenderArgs
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $info.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"
    $script:Sender = [System.Diagnostics.Process]::Start($info)
    $script:Sender.StandardInput.AutoFlush = $true
    $script:SenderStarts += 1
}

function Stop-Sender {
    if ($script:Sender) {
        try { $script:Sender.StandardInput.Close() } catch { }
        try { if (-not $script:Sender.WaitForExit(2000)) { $script:Sender.Kill() } } catch { }
        $script:Sender = $null
    }
}

# One action in, one outcome line back (patches/ptt_control_client.py --stdin).
function Invoke-Sender {
    param([string]$Action)
    try {
        if (-not $script:Sender -or $script:Sender.HasExited) {
            if ($script:Sender) { Log "sender ended; starting it again" "Yellow" }
            Start-Sender
        }
        $script:Sender.StandardInput.WriteLine($Action)
        $reply = $script:Sender.StandardOutput.ReadLineAsync()
        # The sender gives up on the pipe after 0.5 s; 5 s covers a cold start.
        if (-not $reply.Wait(5000)) {
            Stop-Sender
            return "failed: the sender did not answer within 5 s"
        }
        if ($null -eq $reply.Result) { Stop-Sender; return "failed: the sender ended" }
        return $reply.Result.Trim()
    } catch {
        Stop-Sender
        return "failed: " + $_.Exception.Message
    }
}

# Log why push-to-talk is inert once, not on every key: only when it changes.
function Send-SenderAction {
    param([string]$Action)
    $result = Invoke-Sender $Action
    if ($result -eq "none") { return $null }
    if ($result -ne $script:Outcome) {
        switch -Wildcard ($result) {
            "sent"       { if ($script:Outcome) { Log "control channel reachable" "Green" } }
            "no-channel" { Log "no control pipe: no voice turn is running, so push-to-talk does nothing until one is" "DarkGray" }
            "busy"       { Log "control pipe busy; command dropped" "Yellow" }
            default      { Log ("control pipe " + $result) "Yellow" }
        }
        $script:Outcome = $result
    }
    return ($result -eq "sent")
}

function Send-Action {
    param([string]$Action)
    if ($Native) { return Send-SenderAction $Action }
    return Send-RelayAction $Action
}

if ($SendAction.Count -gt 0) {
    try {
        foreach ($item in $SendAction) {
            foreach ($action in ($item -split ",")) {
                $action = $action.Trim()
                if (-not $action) { continue }
                $ok = Send-Action $action
                if ($null -ne $ok) { Log ("{0} sent={1}" -f $action, $ok) }
            }
        }
    } finally {
        Stop-Sender
    }
    exit 0
}

if ($Native) {
    try {
        Start-Sender
    } catch {
        Log ("FATAL: cannot start the sender " + $SenderExe + " -- " + $_.Exception.Message) "Red"
        exit 1
    }
}

Write-Host ""
Write-Host "  VoiceMode push-to-talk listener" -ForegroundColor Cyan
if ($Native) {
Write-Host "  sender       : $SenderExe $SenderArgs"
} else {
Write-Host "  relay        : $RelayHost`:$Port"
}
Write-Host "  hotkey       : Ctrl+Space"
Write-Host "  hold after   : ${HoldThresholdMs}ms"
Write-Host "  active while : a terminal process has focus"
Write-Host "  Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

# The relay is always up, so probe it. The control pipe exists only during a
# voice turn, so there is nothing to probe natively.
if (-not $Native) {
if (Send-Action "press") {
    Log "relay reachable; watching for Ctrl+Space" "Green"
} else {
    Write-Host "  relay NOT reachable at $RelayHost`:$Port" -ForegroundColor Yellow
    Write-Host "  In WSL run:  VOICEMODE_PTT_HOST=0.0.0.0 ~/git/voicemode-local/.venv/bin/python -m voice_mode.ptt_relay" -ForegroundColor Yellow
}
} else {
    Log "native: sending through $SenderExe; watching for Ctrl+Space" "Green"
}

$down        = $false
$pressedAt   = $null
$holdFired   = $false
$lastSeen    = $false

try {
    while ($true) {
        Start-Sleep -Milliseconds 50
        $isDown = ([VmlKey]::Down($VK_CONTROL)) -and ([VmlKey]::Down($VK_SPACE))
        if ($isDown -ne $lastSeen) { Log ("keys ctrl+space = " + $isDown) "DarkGray"; $lastSeen = $isDown }

        if ($isDown -and -not $down) {
            if (-not (Test-TerminalFocused)) {
                Log "ignored: foreground is not a terminal" "Yellow"
                continue
            }
            $down      = $true
            $pressedAt = Get-Date
            $holdFired = $false
        }
        elseif ($isDown -and $down -and -not $holdFired) {
            if (((Get-Date) - $pressedAt).TotalMilliseconds -ge $HoldThresholdMs) {
                $holdFired = $true
                $ok = Send-Action "hold_start"
                Log ("hold_start  (mic open)  sent=" + $ok) "Green"
            }
        }
        elseif (-not $isDown -and $down) {
            $down = $false
            if ($holdFired) {
                $ok = Send-Action "hold_release"
                Log ("hold_end    (sending)  sent=" + $ok) "Green"
            } else {
                $ok = Send-Action "short_press"
                Log ("short_press  sent=" + $ok) "DarkCyan"
            }
        }
    }
} finally {
    if ($down -and $holdFired) { [void](Send-Action "hold_release") }
    if ($Native) { Stop-Sender }
    Write-Host "`n  listener stopped." -ForegroundColor DarkGray
}
