<#
.SYNOPSIS
    Windows-side push-to-talk listener for VoiceMode Local on WSL2.

.DESCRIPTION
    Watches for Ctrl+Space and forwards press / hold / release to the WSL-side
    relay (patches/ptt_relay.py), which hands them to voice-mode's control
    socket.

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

.EXAMPLE
    .\ptt-listener-windows.ps1
    .\ptt-listener-windows.ps1 -HoldThresholdMs 300 -RelayHost 172.24.32.135

.NOTES
    Ctrl+C to stop. Nothing is installed and nothing persists.
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$RelayHost = "127.0.0.1",
    [int]$HoldThresholdMs = 400
)

$ErrorActionPreference = "Stop"

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

function Send-Action {
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

Write-Host ""
Write-Host "  VoiceMode push-to-talk listener" -ForegroundColor Cyan
Write-Host "  relay        : $RelayHost`:$Port"
Write-Host "  hotkey       : Ctrl+Space"
Write-Host "  hold after   : ${HoldThresholdMs}ms"
Write-Host "  active while : a terminal process has focus"
Write-Host "  Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

if (Send-Action "press") {
    Write-Host "  relay reachable." -ForegroundColor Green
} else {
    Write-Host "  relay NOT reachable at $RelayHost`:$Port" -ForegroundColor Yellow
    Write-Host "  In WSL run:  VOICEMODE_PTT_HOST=0.0.0.0 ~/git/voicemode-local/.venv/bin/python -m voice_mode.ptt_relay" -ForegroundColor Yellow
}

$down        = $false
$pressedAt   = $null
$holdFired   = $false

try {
    while ($true) {
        Start-Sleep -Milliseconds 50
        $isDown = ([VmlKey]::Down($VK_CONTROL)) -and ([VmlKey]::Down($VK_SPACE))

        if ($isDown -and -not $down) {
            if (-not (Test-TerminalFocused)) { continue }   # not a terminal
            $down      = $true
            $pressedAt = Get-Date
            $holdFired = $false
        }
        elseif ($isDown -and $down -and -not $holdFired) {
            if (((Get-Date) - $pressedAt).TotalMilliseconds -ge $HoldThresholdMs) {
                $holdFired = $true
                [void](Send-Action "hold_start")
                Write-Host ("  {0:HH:mm:ss}  hold_start  (mic open)" -f (Get-Date)) -ForegroundColor Green
            }
        }
        elseif (-not $isDown -and $down) {
            $down = $false
            if ($holdFired) {
                [void](Send-Action "hold_release")
                Write-Host ("  {0:HH:mm:ss}  hold_end    (sending)" -f (Get-Date)) -ForegroundColor Green
            } else {
                [void](Send-Action "short_press")
                Write-Host ("  {0:HH:mm:ss}  short_press" -f (Get-Date)) -ForegroundColor DarkCyan
            }
        }
    }
} finally {
    if ($down -and $holdFired) { [void](Send-Action "hold_release") }
    Write-Host "`n  listener stopped." -ForegroundColor DarkGray
}
