<#
.SYNOPSIS
    Windows window for the voice pill: a small always-on-top WebView2 page.

.DESCRIPTION
    Hosts ui/pill in a borderless window and runs patches/voice_pill.py inside WSL
    as a child of wsl.exe, speaking JSON lines over stdin/stdout. No network port.

    The window never takes focus: push-to-talk only fires while a terminal has it.
    Everything is C# through Add-Type because stdout arrives on a background thread,
    and a PowerShell delegate has no runspace there. An idle wrapper runs no timers.

    First run downloads Microsoft.Web.WebView2 from nuget.org, checks the pinned
    SHA-256, and unpacks two DLLs into %LOCALAPPDATA%\voicemode-local\webview2.
    Nothing is installed machine-wide. The WebView2 Runtime must already exist.

.PARAMETER Distro
    WSL distro. Default: taken from this script's \\wsl.localhost\<distro>\ path.
.PARAMETER Repo
    The repo's Linux path. Default: taken from this script's path.
.PARAMETER BackendCommand
    Command line run through wsl.exe --exec. Default: the repo's voice_pill.py.
.PARAMETER Force
    Run even if VOICEMODE_PILL is not true.
.PARAMETER InstallAutostart
    Write voicemode-pill.cmd to the Startup folder, then exit.
.PARAMETER RemoveAutostart
    Delete that file, then exit.
.PARAMETER StartupDir
    Use this folder instead of the real Startup folder.

.EXAMPLE
    powershell.exe -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\me\git\voicemode-local\pill-windows.ps1 -InstallAutostart
#>
[CmdletBinding()]
param(
    [string]$Distro = "",
    [string]$Repo = "",
    [string]$BackendCommand = "",
    [switch]$Force,
    [switch]$InstallAutostart,
    [switch]$RemoveAutostart,
    [string]$StartupDir = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$WebView2Version = "1.0.4314.35"
# nuget.org's catalog SHA-512 for this package matched the bytes this hash came from.
$WebView2Sha256 = "90778DEB1B77E36292828B4B5833C9255C27533F40E0DDD55891AE718944EDC4"

$Local = Join-Path $env:LOCALAPPDATA "voicemode-local"
[void](New-Item -ItemType Directory -Force -Path $Local)
# Local on purpose: writing to \\wsl.localhost would boot a terminated distro.
$LogFile = Join-Path $Local "pill-window.log"
function Log {
    param([string]$Text)
    $line = "{0}  {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $Text
    Write-Host "  $line"
    try { $line | Out-File -Append -FilePath $LogFile -Encoding utf8 } catch { }
}

$ScriptPath = $MyInvocation.MyCommand.Path
$ScriptDir = Split-Path -Parent $ScriptPath

if ($InstallAutostart -or $RemoveAutostart) {
    if (-not $StartupDir) { $StartupDir = [Environment]::GetFolderPath("Startup") }
    $entry = Join-Path $StartupDir "voicemode-pill.cmd"
    if ($RemoveAutostart) {
        Remove-Item -Force -ErrorAction SilentlyContinue $entry
        Log "autostart removed: $entry"
    } else {
        $cmd = "@start `"`" /min powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptPath`""
        [IO.File]::WriteAllText($entry, $cmd + "`r`n", [Text.Encoding]::ASCII)
        Log "autostart installed: $entry"
    }
    exit 0
}

function Get-WebView2 {
    $dir = Join-Path (Join-Path $Local "webview2") $WebView2Version
    if (Test-Path (Join-Path $dir "WebView2Loader.dll")) { return $dir }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $url = "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$WebView2Version/microsoft.web.webview2.$WebView2Version.nupkg"
    $pkg = Join-Path $Local "webview2.nupkg.download"
    Log "fetching $url"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $pkg
    $hash = (Get-FileHash -Algorithm SHA256 -Path $pkg).Hash
    if ($hash -ne $WebView2Sha256) {
        Remove-Item -Force $pkg
        throw "WebView2 package failed its checksum: got $hash"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "x64" }
    $tmp = "$dir.tmp"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    [void](New-Item -ItemType Directory -Force -Path $tmp)
    $zip = [IO.Compression.ZipFile]::OpenRead($pkg)
    try {
        foreach ($want in @("lib/net462/Microsoft.Web.WebView2.Core.dll", "runtimes/win-$arch/native/WebView2Loader.dll")) {
            $e = $zip.GetEntry($want)
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, (Join-Path $tmp $e.Name))
        }
    } finally { $zip.Dispose() }
    Move-Item $tmp $dir  # last, so a half-finished unpack never looks installed
    Remove-Item -Force $pkg
    return $dir
}

$mutex = $null
try {
    if ([Threading.Thread]::CurrentThread.ApartmentState -ne "STA") { throw "needs an STA thread: run with powershell.exe" }
    $created = $false
    $mutex = New-Object Threading.Mutex($true, "Local\VoiceModePillWindow", [ref]$created)
    if (-not $created) { Log "another pill window is running; exiting"; $mutex = $null; exit 0 }

    if ($ScriptDir -match '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)(\\.*)$') {
        if (-not $Distro) { $Distro = $Matches[1] }
        if (-not $Repo) { $Repo = $Matches[2] -replace '\\', '/' }
    }
    if (-not $Distro -or (-not $Repo -and -not $BackendCommand)) { throw "pass -Distro and -Repo when not run from a \\wsl.localhost path" }
    if (-not $BackendCommand) { $BackendCommand = "`"$Repo/.venv/bin/python`" `"$Repo/patches/voice_pill.py`" --stdio" }

    # A local copy: the page must keep working while the distro is down.
    $ui = Join-Path $Local "ui"
    if (Test-Path $ui) { Remove-Item -Recurse -Force $ui }
    Copy-Item -Recurse (Join-Path $ScriptDir "ui\pill") $ui

    $wv2 = Get-WebView2
    $core = Join-Path $wv2 "Microsoft.Web.WebView2.Core.dll"
    Set-Location $Local  # the C# compiler cannot start in a UNC directory
    Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Web.Extensions, System.Drawing
    Add-Type -Path $core
    Add-Type -ReferencedAssemblies @("PresentationFramework", "PresentationCore", "WindowsBase", "System.Xaml",
        "System.Web.Extensions", "System.Drawing", $core) -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;

public class VmlPill {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] public struct MONITORINFO { public int cbSize; public RECT rcMonitor, rcWork; public int dwFlags; }
    [StructLayout(LayoutKind.Sequential)] public struct APPBARDATA { public int cbSize; public IntPtr hWnd; public uint uCallbackMessage, uEdge; public RECT rc; public IntPtr lParam; }
    [DllImport("user32.dll")] static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll")] static extern int SetWindowLong(IntPtr h, int i, int v);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int ht, uint f);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] static extern int SetWindowRgn(IntPtr h, IntPtr r, bool redraw);
    [DllImport("gdi32.dll")] static extern IntPtr CreateRoundRectRgn(int a, int b, int c, int d, int e, int f);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int k);
    [DllImport("user32.dll")] static extern IntPtr MonitorFromPoint(POINT p, uint f);
    [DllImport("user32.dll")] static extern bool GetMonitorInfo(IntPtr m, ref MONITORINFO i);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern uint RegisterWindowMessage(string s);
    [DllImport("shell32.dll")] static extern UIntPtr SHAppBarMessage(uint m, ref APPBARDATA d);

    const uint NOACTIVATE = 0x10, NOSIZE = 0x1, NOZORDER = 0x4;
    const int WIDTH = 320, DOT = 16;
    static readonly IntPtr TOPMOST = new IntPtr(-1);
    const string HOST = "pill.voicemode.example";
    const string BRIDGE = @"
window.pillTransport={send:function(j){window.chrome.webview.postMessage(j)}};
window.chrome.webview.addEventListener('message',function(e){if(window.pillReceive)window.pillReceive(e.data)});
document.addEventListener('DOMContentLoaded',function(){
  var r=document.documentElement,c=document.getElementById('card');
  r.style.background=getComputedStyle(r).getPropertyValue('--bg');
  new ResizeObserver(function(){if(c.offsetHeight)window.chrome.webview.postMessage(JSON.stringify({window:{height:c.offsetHeight}}))}).observe(c);
});";

    string distro, command, local, posFile, logFile; bool force;
    Window win; IntPtr hwnd; CoreWebView2Controller ctl; Dispatcher ui;
    StreamWriter stdin; Process proc; DateTime startedAt;
    JavaScriptSerializer json = new JavaScriptSerializer();
    double backoff = 1; bool closing, started, hadOutput; int exitCode;
    bool snapshotVisible, dismissed; string mode = "pill", side = "right"; int cssHeight = 46;
    bool hasPos; int posX, posY; bool appbar; uint abMsg;
    DispatcherTimer dragTimer; int dragDx, dragDy; bool moved;

    void Log(string s) {
        try { File.AppendAllText(logFile, DateTime.Now.ToString("HH:mm:ss.fff") + "  " + s + Environment.NewLine); } catch (IOException) { }
    }

    // 1 on, 0 off, -1 could not tell (wsl failed). Blocks: never call on the UI thread.
    public static int PillEnabled(string distro) {
        try {
            var psi = new ProcessStartInfo("wsl.exe", "-d " + distro + " --exec bash -c \"grep -s ^VOICEMODE_PILL= ~/.voicemode/voicemode.env | tail -1\"");
            psi.UseShellExecute = false; psi.RedirectStandardOutput = true; psi.CreateNoWindow = true;
            using (var p = Process.Start(psi)) {
                string o = p.StandardOutput.ReadToEnd();
                if (!p.WaitForExit(60000) || p.ExitCode != 0) return -1;
                return o.Trim() == "VOICEMODE_PILL=true" ? 1 : 0;
            }
        } catch (Exception) { return -1; }
    }

    public static int Run(string distro, string command, string local, string loader, bool force) {
        return new VmlPill().Main(distro, command, local, loader, force);
    }

    int Main(string d, string c, string l, string loader, bool f) {
        distro = d; command = c; local = l; force = f;
        posFile = Path.Combine(local, "pill-position.txt"); logFile = Path.Combine(local, "pill-window.log");
        LoadPosition();
        CoreWebView2Environment.SetLoaderDllFolderPath(loader);
        win = new Window { Title = "Voice pill", WindowStyle = WindowStyle.None, ResizeMode = ResizeMode.NoResize,
            ShowInTaskbar = false, ShowActivated = false, Topmost = true, Width = WIDTH, Height = 46 };
        hwnd = new WindowInteropHelper(win).EnsureHandle();  // an HWND without ever showing it
        SetWindowLong(hwnd, -20, GetWindowLong(hwnd, -20) | 0x08000000 | 0x80);  // NOACTIVATE | TOOLWINDOW
        abMsg = RegisterWindowMessage("VoiceModePillAppBar");
        HwndSource.FromHwnd(hwnd).AddHook(WndProc);
        ui = Dispatcher.CurrentDispatcher;
        ui.UnhandledException += (s, e) => { Log("FATAL: " + e.Exception); e.Handled = true; exitCode = 2; Shutdown(); };
        AppDomain.CurrentDomain.ProcessExit += (s, e) => AppBarOff();
        Console.CancelKeyPress += (s, e) => { e.Cancel = true; Post(Shutdown); };
        win.Closed += (s, e) => { if (!closing) Shutdown(); };
        // `voicemode-switch pill off` sets this: the one stop path that needs no backend.
        var stop = new EventWaitHandle(false, EventResetMode.AutoReset, @"Local\VoiceModePillStop");
        Background(() => { stop.WaitOne(); Log("stop requested"); Post(Shutdown); });
        Post(Init);
        Dispatcher.Run();
        return exitCode;
    }

    void Post(Action a) { ui.BeginInvoke(a); }

    async void Init() {
        try {
            var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(local, "webview2-data"));
            ctl = await env.CreateCoreWebView2ControllerAsync(hwnd);
            ctl.IsVisible = false;
            var web = ctl.CoreWebView2;
            var st = web.Settings;
            st.AreDevToolsEnabled = false; st.AreDefaultContextMenusEnabled = false; st.IsStatusBarEnabled = false;
            st.AreBrowserAcceleratorKeysEnabled = false; st.IsZoomControlEnabled = false; st.IsPinchZoomEnabled = false;
            web.SetVirtualHostNameToFolderMapping(HOST, Path.Combine(local, "ui"), CoreWebView2HostResourceAccessKind.Deny);
            web.NavigationStarting += (s, e) => { if (!e.Uri.StartsWith("https://" + HOST + "/")) e.Cancel = true; };
            web.NewWindowRequested += (s, e) => { e.Handled = true; };
            web.PermissionRequested += (s, e) => { e.State = CoreWebView2PermissionState.Deny; };  // never the mic
            web.ProcessFailed += (s, e) => { Log("webview process failed: " + e.ProcessFailedKind); if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited) web.Reload(); };
            web.WebMessageReceived += OnPageMessage;
            await web.AddScriptToExecuteOnDocumentCreatedAsync(BRIDGE);
            web.NavigationCompleted += (s, e) => { if (!started) { started = true; StartBackend(); } };
            web.Navigate("https://" + HOST + "/index.html");
        } catch (Exception e) { Log("FATAL: webview: " + e); exitCode = 2; Shutdown(); }
    }

    IntPtr WndProc(IntPtr h, int msg, IntPtr w, IntPtr l, ref bool handled) {
        if (msg == 0x21) { handled = true; return new IntPtr(3); }  // WM_MOUSEACTIVATE: MA_NOACTIVATE
        if (msg == 0x7E) Apply();                                   // WM_DISPLAYCHANGE
        if (abMsg != 0 && msg == abMsg && w.ToInt64() == 1 && appbar) Apply();  // ABN_POSCHANGED
        return IntPtr.Zero;
    }

    // -- backend -------------------------------------------------------------
    void StartBackend() {
        if (closing) return;
        var psi = new ProcessStartInfo("wsl.exe", "-d " + distro + " --exec " + command);
        psi.UseShellExecute = false; psi.CreateNoWindow = true;
        psi.RedirectStandardInput = psi.RedirectStandardOutput = psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = psi.StandardErrorEncoding = Encoding.UTF8;
        // Process.Start writes this encoding's BOM into the child's stdin at once.
        try { Console.InputEncoding = new UTF8Encoding(false); } catch (IOException) { }
        try {
            proc = Process.Start(psi);
        } catch (Exception e) { Log("backend failed to start: " + e.Message); proc = null; Retry(); return; }
        stdin = new StreamWriter(proc.StandardInput.BaseStream, new UTF8Encoding(false));
        stdin.AutoFlush = true;
        startedAt = DateTime.UtcNow;
        Log("backend started: pid " + proc.Id);
        var p = proc;
        Background(() => { string e; while ((e = p.StandardError.ReadLine()) != null) Log("backend: " + e); });
        Background(() => {
            int lines = 0;
            while (true) {
                string line = p.StandardOutput.ReadLine();
                if (line == null) break;
                lines++;
                Post(() => OnLine(line));
            }
            p.WaitForExit();
            int code = p.ExitCode, n = lines;
            bool off = !force && !closing && PillEnabled(distro) == 0;
            Post(() => OnExit(code, n, off));
        });
    }

    static void Background(ThreadStart f) { var t = new Thread(f); t.IsBackground = true; t.Start(); }

    void OnExit(int code, int lines, bool off) {
        stdin = null; proc = null;
        if (closing) return;
        Log("backend exited: code " + code + ", " + lines + " lines");
        if (off) { Log("VOICEMODE_PILL is off; exiting"); Shutdown(); return; }
        // A second instance exits 1 before printing anything, and so does a failed
        // import: retrying helps neither. Exit 1 after real output is a crash, or the
        // distro going down, and those are what the restart is for.
        if (code == 1 && lines == 0) {
            if (!hadOutput) {
                // Probably a broken venv. A window that never worked must not sit hidden
                // holding the mutex: exit, so the next `pill on` can start cleanly.
                Log("backend refused to start (exit 1) and has never run; exiting");
                exitCode = 1; Shutdown(); return;
            }
            Log("backend refused to start (exit 1); not restarting");  // the page goes Disconnected
            return;
        }
        if ((DateTime.UtcNow - startedAt).TotalSeconds >= 30) backoff = 1;
        Retry();
    }

    void Retry() {
        Log("restarting backend in " + backoff + " s");
        var t = new DispatcherTimer { Interval = TimeSpan.FromSeconds(backoff) };
        backoff = Math.Min(backoff * 2, 30);
        t.Tick += (s, e) => {
            t.Stop();
            Background(() => {
                bool off = !force && !closing && PillEnabled(distro) == 0;
                Post(() => { if (off) { Log("VOICEMODE_PILL is off; exiting"); Shutdown(); } else StartBackend(); });
            });
        };
        t.Start();
    }

    void OnLine(string line) {
        hadOutput = true;
        if (closing || ctl == null) return;
        Dictionary<string, object> m;
        try { m = json.DeserializeObject(line) as Dictionary<string, object>; } catch (ArgumentException) { return; }
        if (m == null) return;
        object v;
        if (m.TryGetValue("visible", out v) && v is bool && (bool)v != snapshotVisible) { snapshotVisible = (bool)v; Apply(); }
        ctl.CoreWebView2.PostWebMessageAsString(line);
    }

    void OnPageMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e) {
        string text = e.TryGetWebMessageAsString();
        Dictionary<string, object> m;
        try { m = json.DeserializeObject(text) as Dictionary<string, object>; } catch (ArgumentException) { return; }
        if (m == null) return;
        object v;
        if (m.TryGetValue("window", out v)) { var w = v as Dictionary<string, object>; if (w != null) OnWindowMessage(w); return; }
        if (m.ContainsKey("cmd") && stdin != null) {
            try { stdin.WriteLine(text); } catch (IOException) { } catch (ObjectDisposedException) { }
        }
    }

    void OnWindowMessage(Dictionary<string, object> w) {
        object v;
        if (w.ContainsKey("drag")) { StartDrag(); return; }
        if (w.TryGetValue("height", out v) && v is int) { cssHeight = (int)v; Apply(); return; }
        if (w.TryGetValue("mode", out v) && v is string) {
            string next = (string)v;
            dismissed = next == "hidden";
            if (!dismissed) mode = next;
            object sv;
            side = w.TryGetValue("side", out sv) && (sv as string) == "left" ? "left" : "right";
            Apply();
        }
    }

    // -- window --------------------------------------------------------------
    int Scale(int css) { return (int)Math.Round(css * GetDpiForWindow(hwnd) / 96.0); }

    MONITORINFO MonitorAt(int x, int y) {
        var p = new POINT { x = x, y = y };
        var i = new MONITORINFO { cbSize = Marshal.SizeOf(typeof(MONITORINFO)) };
        GetMonitorInfo(MonitorFromPoint(p, 2), ref i);  // nearest, so an unplugged monitor is survivable
        return i;
    }

    void Apply() {
        if (closing || ctl == null) return;
        if (!snapshotVisible || dismissed) {
            AppBarOff();
            ShowWindow(hwnd, 0);
            ctl.IsVisible = false;
            return;
        }
        if (mode == "docked") { AppBarOn(); } else { AppBarOff(); Place(); }
        ShowWindow(hwnd, 4);  // SW_SHOWNOACTIVATE
        ctl.IsVisible = true;
    }

    void Move(RECT r) {
        int w = r.right - r.left, h = r.bottom - r.top;
        SetWindowPos(hwnd, TOPMOST, r.left, r.top, w, h, NOACTIVATE);
        ctl.Bounds = new System.Drawing.Rectangle(0, 0, w, h);
    }

    void Place() {
        int w = Scale(mode == "dot" ? DOT : WIDTH), h = Scale(mode == "dot" ? DOT : cssHeight);
        var a = (hasPos ? MonitorAt(posX, posY) : MonitorAt(0, 0)).rcWork;
        int x = hasPos ? posX : a.right - w - Scale(16), y = hasPos ? posY : a.bottom - Scale(46) - Scale(16);
        x = Math.Max(a.left, Math.Min(x, a.right - w));
        y = Math.Max(a.top, Math.Min(y, a.bottom - h));
        Move(new RECT { left = x, top = y, right = x + w, bottom = y + h });
        int round = mode == "dot" ? w : Scale(32);
        SetWindowRgn(hwnd, CreateRoundRectRgn(0, 0, w + 1, h + 1, round, round), true);  // the OS owns the region now
    }

    void AppBarOn() {
        var d = new APPBARDATA { cbSize = Marshal.SizeOf(typeof(APPBARDATA)), hWnd = hwnd, uCallbackMessage = abMsg };
        if (!appbar) { SHAppBarMessage(0, ref d); appbar = true; }  // ABM_NEW
        int w = Scale(WIDTH);
        var mon = (hasPos ? MonitorAt(posX, posY) : MonitorAt(0, 0)).rcMonitor;
        d.uEdge = side == "left" ? 0u : 2u;
        d.rc = mon;
        if (side == "left") d.rc.right = mon.left + w; else d.rc.left = mon.right - w;
        SHAppBarMessage(2, ref d);  // ABM_QUERYPOS: the shell trims it around other bars
        if (side == "left") d.rc.right = d.rc.left + w; else d.rc.left = d.rc.right - w;
        SHAppBarMessage(3, ref d);  // ABM_SETPOS
        Move(d.rc);
        SetWindowRgn(hwnd, IntPtr.Zero, true);
    }

    void AppBarOff() {
        if (!appbar) return;
        appbar = false;
        var d = new APPBARDATA { cbSize = Marshal.SizeOf(typeof(APPBARDATA)), hWnd = hwnd };
        SHAppBarMessage(1, ref d);  // ABM_REMOVE
    }

    // Own drag loop: a drag started inside WebView2 has its mouse capture in another
    // process, and the system move loop would activate the window.
    void StartDrag() {
        if (dragTimer != null || mode == "docked") return;
        POINT c; GetCursorPos(out c);
        var r = CurrentOrigin();
        dragDx = c.x - r.x; dragDy = c.y - r.y; moved = false;
        dragTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(16) };
        dragTimer.Tick += (s, e) => {
            POINT p; GetCursorPos(out p);
            if (p.x - dragDx != r.x || p.y - dragDy != r.y) {
                r.x = p.x - dragDx; r.y = p.y - dragDy; moved = true;
                SetWindowPos(hwnd, IntPtr.Zero, r.x, r.y, 0, 0, NOACTIVATE | NOSIZE | NOZORDER);
            }
            if ((GetAsyncKeyState(1) & 0x8000) == 0) {
                dragTimer.Stop(); dragTimer = null;
                if (moved) { posX = r.x; posY = r.y; hasPos = true; SavePosition(); Apply(); }
            }
        };
        dragTimer.Start();
    }

    POINT CurrentOrigin() {
        RECT r; GetWindowRect(hwnd, out r);
        return new POINT { x = r.left, y = r.top };
    }
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);

    void LoadPosition() {
        try {
            var p = File.ReadAllText(posFile).Split(' ');
            hasPos = int.TryParse(p[0], out posX) & int.TryParse(p[1], out posY);
        } catch (Exception) { hasPos = false; }
    }

    void SavePosition() {
        try { File.WriteAllText(posFile, posX + " " + posY); } catch (IOException e) { Log("position not saved: " + e.Message); }
    }

    void Shutdown() {
        if (closing) return;
        closing = true;
        AppBarOff();
        try { if (stdin != null) stdin.Close(); } catch (IOException) { }  // EOF ends the backend and lifts its Pause
        try { if (proc != null && !proc.WaitForExit(2000)) proc.Kill(); } catch (Exception) { }
        try { if (ctl != null) ctl.Close(); } catch (Exception e) { Log("webview close failed: " + e.Message); }
        win.Close();
        ui.InvokeShutdown();
    }
}
'@

    if (-not $Force) {
        $on = [VmlPill]::PillEnabled($Distro)
        if ($on -eq 0) { Log "VOICEMODE_PILL is not true; exiting"; exit 0 }
        if ($on -lt 0) { Log "could not read VOICEMODE_PILL from WSL; starting anyway" }
    }
    Log "pill window starting: distro=$Distro backend=$BackendCommand"
    $code = [VmlPill]::Run($Distro, $BackendCommand, $Local, $wv2, [bool]$Force)
    Log "pill window stopped: code $code"
    exit $code
} catch {
    Log ("FATAL: " + $_.Exception.Message)
    exit 1
} finally {
    if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
}
