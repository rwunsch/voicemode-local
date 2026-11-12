# Voice pill: Windows window

> **TL;DR:** `pill-windows.ps1` shows `ui/pill` in a small always-on-top window that never
> takes focus, and runs `patches/voice_pill.py` inside WSL over stdin/stdout. Tested on the
> Windows desktop with a fake backend and in real voice turns (2026-11-05: two sessions, then one
> real agent, n=1 each). Off by default: turn it on with `voicemode-switch pill on`.

| Field | Value |
|---|---|
| Status | Released, off by default. WSL backend; a native Windows mode (no WSL) since 2026-11-06, started by the native launcher. Open checks are listed below |
| Last updated | 2026-11-11 (v1.1.1: the voice server starts and stops the window) |
| Measured on | Windows build 26200, PowerShell 5.1, WebView2 Runtime 156, one 1920x1080 monitor at 100% scale |

## Use

```bash
voicemode-switch pill on       # sets the flag and the control channel, opens the window under WSL
voicemode-switch pill off      # clears the flag and signals the window to close, with or without a backend
voicemode-switch pill status
```

Autostart at login, from Windows (writes `voicemode-pill.cmd` to the Startup folder):

```powershell
powershell.exe -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu\home\<you>\git\voicemode-local\pill-windows.ps1 -InstallAutostart
# undo with -RemoveAutostart
```

The window exits at once unless `VOICEMODE_PILL=true`, so the entry is harmless when off.
The entry repeats every argument the install was given except the autostart ones (`-Distro`,
`-Repo`, `-BackendCommand`, `-BackendExe`, `-BackendArgs`, `-Force`, and `-DataDir` resolved to
a full path). It is UTF-8 without a BOM after `chcp 65001`, so non-ASCII paths survive.

The WSL window reads `VOICEMODE_PILL` from the distro's `~/.voicemode/voicemode.env` (or the old
`.voicemode.env`) by the same rules as native mode and `voice_pill_state`: `export `, quotes,
CRLF and a BOM are allowed, and the last assignment wins. An inline comment stays in the value
there, so `VOICEMODE_PILL=true # on` is off. A distro that does not answer within 60 s is
killed and counts as "could not tell", and the window starts anyway.

`pill on` waits up to 15 s and fails with exit 1 if the window exited with an error. It cannot
detect a failure after those 15 s. "Already running" only means a window holds the lock: that
window may have no backend. `pill off` then `pill on` starts clean.

Files are in `%LOCALAPPDATA%\voicemode-local`: `webview2\` (two DLLs), `webview2-data\`,
`ui\` (copied at each start), `pill-position.txt` and `pill-window.log`.

## Who starts and stops the window (v1.1.1)

Decision (Robert, 2026-11-10): the pill opens with the first voice session and closes
with the last. This applies to every host, FELIX or not. If the user closes it with ✕, it
stays closed until voice starts again from scratch. The voice server itself starts the
window. No launcher starts it any more.

- **Start.** `voice_pill_state.enter()` (on each converse turn) calls
  `voice_mode/pill_autostart.ensure()`. It starts the window only if all of these hold:
  - `VOICEMODE_PILL` is true.
  - `VOICEMODE_PILL_AUTOSTART` is not `off`.
  - There is no `<base>/pill/closed`.
  - The pid in `<base>/pill/backend.json` is not alive.
  - No start was tried in the last 20 s (`autostart.json`).

  Each attempt is logged in `<base>/pill/autostart.log`. The start command depends on the OS:
  - **Native Windows:** `pill-windows.ps1 -BackendExe <python> -DataDir <VOICEMODE_PILL_DATA_DIR>`.
    It is started with breakaway from the server's Job Object, and without it if the job refuses.
  - **WSL:** `Start-Process powershell.exe … pill-windows.ps1 -BackendCommand …`, so the window
    is not a child of the interop call.
  - **macOS:** `<VOICEMODE_PILL_PYTHON> voice_pill_mac.py --backend …`. It needs the pill venv
    that `voicemode-switch pill on` makes.

  `pill-windows.ps1`, `voice_pill_mac.py` and `ui/pill` are copied into the voice_mode
  package by `patches/apply.py`.
- **Sticky close.** ✕ writes `<base>/pill/closed`, and the snapshot then says `visible:
  false`. The marker is removed when a session enters its first turn while no other
  session is live (a fresh start), and when the backend quits.
- **Quit.** The backend never quits in its first 30 s. After that, once there has been no
  live session and no push-to-talk for 20 s, it prints `{"quit": true}` and exits with code 3.
  Both windows close on code 3 and do not restart. `voice_stop` removes the session at once,
  and a dead server's session is pruned by pid.
  `voice_stop` is in `voice_barge_tool`, which upstream hides unless a tool list names it:
  `voicemode-mcp` (and the native launcher) set `converse,service,voice_barge_tool` when no
  tool setting exists. FELIX must include `voice_barge_tool` in any list it passes.
- **Settings.**
  - `VOICEMODE_PILL_DATA_DIR`: the Windows folder. Default `<VOICEMODE_NATIVE_ROOT>\pill`,
    else `<base>/pill/window`. WSL passes none and keeps `%LOCALAPPDATA%\voicemode-local`.
  - `VOICEMODE_PILL_PYTHON`: the backend interpreter natively, or the pill venv python on
    macOS.
  - `voicemode-switch pill on|off` and `voicemode_native.py pill on|off` still open or close
    the window at once.

Validated live on WSL (2026-11-11, n=1 each step, real converse turns after `/mcp` reconnect):
- The turn started the window: launch 11:35:21, backend up 11:35:28.
- ✕ wrote the marker, and the next turn did not reopen the window. That turn came from the
  same session; a second session's turn is covered by unit tests only.
- `voice_stop` at about 11:37:48, then the backend exited with code 3 at 11:38:07 and the window
  stopped with code 0. The marker and `backend.json` were removed.
- A fresh start reopened the window: launch 11:38:49, backend up 11:38:56, about 7 s.

Validated natively on Windows (2026-11-11, n=1 each). Setup: the native runtime's Python,
running a patched copy of its `voice_mode` from `C:\Temp\vml-autostart`, with private base and
data folders. `ensure()` was called directly, not through a converse turn.
- **Plain start.** The starting process exited at once, and the window kept running. The
  backend was up after 3.5 s. With no session, it exited with code 3 at about 30 s (the start
  grace), the window stopped with code 0, and `backend.json` was removed.
- **Inside a Job Object with `BREAKAWAY_OK` and `KILL_ON_JOB_CLOSE`.** Breakaway worked: the
  backend was still alive 3 s after the job closed, and it later quit with code 3.
- **Inside a strict job (`KILL_ON_JOB_CLOSE`, no breakaway).** `autostart.log` shows `breakaway
  refused ([WinError 5] Access is denied); starting inside the job`. The window started, and
  then died with the job, as designed. It is started again on the next turn of a server
  outside that job.

Whether the Copilot app or FELIX hosts use such a job is FELIX-voice's measurement. Not yet
run: a native converse turn through Claude Code, and macOS.

## How it works

- **WebView2 without the WPF control.** The page is hosted through `CoreWebView2Controller`
  on a WPF window's HWND. That allows loading it before the window is first shown. Corners
  come from a window region, because WebView2 does not render in a layered window.
- **C# through `Add-Type`.** Backend output arrives on a background thread, where a
  PowerShell delegate has no runspace. An idle wrapper runs no timers.
- **Package pin.** Microsoft.Web.WebView2 1.0.4314.35 from nuget.org, SHA-256
  `90778deb...44edc4`. nuget.org's catalog SHA-512 for it matched the downloaded bytes.
- **Dragging** polls the cursor while the left button is down, instead of the system move
  loop, because the mouse capture belongs to another process.
- **Restarts** back off 1, 2, 4, 8, 16, then 30 s, resetting after a 30 s run.
- **Exit code 1 with no output means no restart.** That is what a second instance does, and
  what `wsl.exe` returns for a missing venv. If no backend has ever produced output, the
  window exits and logs why. If one has, it stays up and the page shows Disconnected. Exit 1
  after output is treated as a crash and restarted, on the expectation that `wsl --terminate`
  may look like that (not measured).
- **A command with no backend to take it is not kept.** While the backend restarts, or after
  its pipe broke, the window logs it and posts `{"window":{"dropped":"<cmd>"}}` to the page,
  so the page can show Disconnected at once. It is never replayed: a late Stop could land on
  a turn it was not meant for.
- **Push-to-talk** (2026-11-07): while the key is down the pill shows **Holding…** (phase
  `holding`, from the `holding` field patch_ptt_instant adds to the server's status reply,
  above Speaking and Listening, green like Listening). The keys hint reads `tap: stop, hold:
  talk` on every platform: key down cuts the reply and opens the mic, a release within 0.8 s
  stops (back to text), a later one sends what you said.
- **Wake and the button layout** (2026-11-07, Robert's decision: a play and a stop button
  together make no sense, and Skip is dropped). The pill has one main button in the leftmost
  spot that switches state, and Pause beside it:

  | State | Main button (spot 1) | Spot 2 |
  |---|---|---|
  | between turns (idle, between turns, stopped), a last voice session exists, wake enabled | **Wake**: a line-art sunrise (half sun on a horizon with short rays, inline SVG), title `Wake <label>` | hidden |
  | a voice turn is running | **Stop** (square) | Pause (pause icon) |
  | paused | Stop | Resume, shown as the play/pause icon (not a plain play) |

  With no session to wake the main button stays Stop (disabled without a floor). There is no
  Skip button; the backend `interrupt` command still works for other callers (push-to-talk).
  Wake sends `{"cmd":"wake"}` (the old `{"cmd":"play"}` still works); the backend calls
  `session_wake.wake(<claude pid>, "wake")`, which asks that Claude Code session, through its
  own session socket, to call converse with `message="Listening."` and
  `wait_for_response=true`. The woken session therefore SPEAKS "Listening." and then
  listens (a silent wake was measured on the Mac 2026-11-07, n=1, beeps on). While the wake is
  pending the state text shows `Waking <label>... listen for "Listening."` (until the
  session's registry status turns `busy` or 5 s pass), then `Woke <label>`, or
  `Can't wake <label>: <why>` (busy, waiting for a permission answer, rate limited, ...). If
  the status has not turned `busy` within 5 s it shows `No answer from <label>`. Like an
  idle push-to-talk hold, Wake only wakes: talk after you hear "Listening.", which took 6 s
  and 11 s to arrive when measured live 2026-11-07 (n=2). (The pill shows its own Wake
  wakes only; a push-to-talk wake is not shown.) The last voice session is
  the newest live conch holder in voice-mode's events log, then the pill's own session
  records; its voice-server pid is mapped up its parents to the claude pid. Hidden when
  `VOICEMODE_SESSION_WAKE=off`. Same rules and caveats as the push-to-talk wake
  (docs/windows-push-to-talk): undocumented protocol, idle sessions only, one per 5 s,
  logged to `~/.voicemode/logs/session-wake.log`, Windows named pipe UNVERIFIED.
- **Stopping** needs no backend: the window waits on a `Local\VoiceModePillStop` event, which
  `pill off` sets through `powershell.exe`. The flag is also read when a backend exits and
  before each retry.

## Windows native (no WSL)

The voice server (since v1.1.1; before that the native launcher,
`scripts/windows/voicemode_native.py`, see `docs/windows-native/README.md`) starts the same
window in native mode when `VOICEMODE_PILL=true`:

```
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File pill-windows.ps1 ^
  -BackendExe <runtime>\.venv\Scripts\python.exe -BackendArgs "-m voice_mode.voice_pill --stdio" ^
  -DataDir <native root>\pill
```

- **When.** Native mode is chosen when `-BackendExe` is given, or when there is no `-Distro`
  and the script is not on a `\\wsl` path. Every WSL start today passes neither, so it stays
  WSL. Passing both `-Distro` and `-BackendExe` is refused.
- **Backend.** Started directly: `-BackendExe` with `-BackendArgs`, one Windows command line.
  No `wsl.exe`, no `python3` lookup. Its working folder is `-DataDir`, so no project
  `.voicemode.env` near the caller is read. Same JSON lines on stdin and stdout, same EOF
  shutdown, same restart rules as WSL.
- **On or off.** `VOICEMODE_PILL` from the window's environment first, then
  `%USERPROFILE%\.voicemode\voicemode.env`, read in the window's own process as UTF-8 (BOM and
  CRLF fine; the last line wins). That is the order and file of `voice_pill_state.setting`,
  which the launcher uses too. `VOICEMODE_BASE_DIR` does not move that file, as on WSL.
- **One pill per install.** Its files (log, position, page copy, WebView2 DLLs,
  `webview2-data`) go to `-DataDir`, required natively. Its mutex and stop event are
  `Local\VoiceModePillWindow-<12 hex>` and `Local\VoiceModePillStop-<12 hex>`, from SHA-256 of
  the folder's full long path (`GetFullPath`, 8.3 names expanded, trailing slashes dropped,
  lowercased, UTF-8), so any spelling of the folder gives the same names. `pill_stop_event(root)` in
  the launcher gives the same stop name; set that event to close the window.
  `pill-windows.ps1 -DataDir <dir> -BackendExe x -PrintNames` prints both names.
- **WSL unchanged.** WSL mode keeps `%LOCALAPPDATA%\voicemode-local` and the two fixed names,
  which `voicemode-switch pill off` relies on.
- **Hidden start.** The launcher starts the window with `CREATE_NO_WINDOW`. It still shows,
  and it does not take focus (measured, below).

## What was checked

Done 2026-11-05 by hand, n=1 unless stated. FB = fake backend (`tests/pill_fake_backend.py`);
RB = real `voice_pill.py` under a fake HOME with a registered dummy session.

| Check | Result |
|---|---|
| WebView2 from PowerShell 5.1 (go/no-go) | Works, including first-run download |
| Wrong checksum | Exit 1, package deleted, no DLL unpacked |
| Foreground unchanged on show, hide, click, drag, dropdown | Same terminal window throughout, read before and after (FB) |
| Not activating, not in taskbar or Alt-Tab | Style bits `0x08000088` (NOACTIVATE, TOOLWINDOW, TOPMOST) read back. Alt-Tab list not looked at |
| Always on top | Another window placed over it: `WindowFromPoint` still returned the pill |
| Drag | Moves during the drag, position saved (FB) |
| Position restored after restart | Same rectangle (n=3 restarts, FB and RB) |
| Hide and show on `visible` | Correct both ways (FB, RB) |
| Dismiss | Stays hidden while `visible` stays true; returns on false then true (FB) |
| Disconnected when the backend dies | Shown; returns on restart (FB) |
| Backoff | 1, 2, 4, 8, 16, 30, 30 s from the log, then recovery (FB) |
| Exit 1 before any output ever (missing venv) | Window exits with code 1, `pill on` reports it with exit 1 (n=2) |
| Exit 1 before output, after earlier output | Stays up, no restart in 30 s, page text read as "Disconnected" through UI Automation (FB) |
| Exit 1 after output | Restarted (FB) |
| Second window | Exits at once |
| Pause released on window close, and on hard kill | `paused.json` gone in both (RB, n=1 each) |
| Backend and WebView2 gone after close and hard kill | No processes left (n=7 closes, n=2 hard kills; a third hard kill checked the backend only) |
| `pill off` closes the window | With a backend (n=1), with a refused backend (n=1), during a backoff wait (n=1), all through the event, fake HOME |
| Flag cleared during a backoff wait, no event | Window exits at the retry, no new backend (FB) |
| `pill on` with a healthy window, and with one already up | "Voice pill on", and "already running" (n=1 each, fake HOME) |

CPU, all window processes together over 20 s on 20 logical CPUs (FB, n=1 unless stated):

| State | % of machine | % of one core |
|---|---|---|
| Visible, idle | 0.10, 0.12 (n=2) | 2.0, 2.3 |
| Hidden | 0.12 | 2.4 |
| Listening | 0.09 | 1.7 |
| Speaking | 0.11 | 2.2 |

The orb first had a pulse animation, removed on 2026-11-05 because audio runs during those
states: listening cost 10.4 and 17.1% of one core with it (n=2), and speaking 14.8% (n=1). State
now shows by colour only. Whether the old or new cost affects stutter was not measured.

## Open: needs Robert's voice

- [ ] Pill appears on a real `converse`
- [ ] States change correctly in a real turn
- [ ] Pause, Resume and Stop work in a real turn; Wake shows the sunrise between turns and the woken session says "Listening."
- [ ] Closing the window while paused releases Pause in a real turn
- [ ] No new stutter under a voice turn (`voicemode-perf`)

## Known issues

- After Stop (or the old Interrupt), the player's stop call was seen blocking for about 6 s and once 36 s, under load 12 to 20 (2026-11-05, n=2). A silent test of the same call returned in about 0.1 s, n=3. Cause unknown; whether sound continued is not recorded.
- No Windows push-to-talk wake test on a real Windows Claude Code yet. A stopped session gets going again when you ask it in its terminal. An agent stayed quiet after Stop, n=1, headless.
- The Interrupt by voice toggle fix has not been retested with a real click.
- The pill's patches install even with the pill off. A turn then does a few extra file operations and leaves one small file per voice session in `~/.voicemode/pill/sessions/`, which nothing removes while the pill is off (final review, read in the code).
- `pill off` leaves `~/.voicemode/pill/beeps.json` in place, and it still decides beeps: delete it to go back to `VOICEMODE_AUDIO_FEEDBACK`. It also leaves `VOICEMODE_CONTROL_CHANNEL_ENABLED=true`, which push-to-talk uses too.
- If every voice turn hangs while Paused and no window is visible, run `voicemode-switch pill off`: it ends the backend, which releases the Pause.
- To pick up a later pill build, rebuild the venv from pristine voice-mode: the patch skips files it has already marked.

## Checked natively (2026-11-06)

`tests/test_pill_windows_native_windows.py`, run by `tests/run_windows_transport_tests.sh`,
on this laptop: 9 tests, all passed in each of 3 runs. Each start uses the launcher's own
command and a process with no window.

- The window shows in 2.6 to 3.9 s (n=6 timed starts). The pill never became the foreground
  window. In one run (n=3) the foreground was a real window, the same before and after; in
  another (n=3) Windows reported no foreground window before or after, so that run only shows
  the pill did not take it.
- Its files land in `-DataDir`; nothing new appears in `%LOCALAPPDATA%\voicemode-local`.
- A second start on the same folder exits 0. A start on another folder runs alongside, and
  each stops only by its own stop event.
- The stop event closes it with exit 0, and the backend sees stdin EOF.
- A backend that exits 3 after output was restarted. Since v1.1.1, code 3 means "no voice
  session left" and closes the window instead; another non-zero code after output still
  restarts. One that exits 1 before any output ends the window with exit 1.
- `VOICEMODE_PILL`: false in the file exits; true in a CRLF file with a BOM runs; false in
  the environment beats true in the file.
- The real backend (`voice_mode.voice_pill --stdio` in the native runtime) starts, takes its
  lock in a private base dir and is ended by EOF, without a restart.

Fix round 1 (2026-11-06) made the focus test count only starts with a real foreground
window (at least 3), and added name checks for relative and 8.3 paths.

Not checked natively: a real voice session shown in the native pill, an elevated voice
server, more than one monitor, and the native switch (task 8).

## Not tested

- `wsl --terminate` and recovery: it would have ended every WSL session on this machine.
- A real login through the Startup folder. The generated `.cmd` was run from a temp folder.
- More than one monitor, a monitor being unplugged, and scales other than 100%.
- Fullscreen apps: the pill stays on top of them, and nothing handles that yet.

## Known limits

- An unhandled Python exception before the first snapshot also exits 1, so it is not retried.
  It appears in `pill-window.log`. Run `pill off`, then `pill on`, to restart.
- Native Windows (not WSL): run voice-mode and the pill at the same level. If the voice
  server runs elevated (as administrator) and the pill does not, the pill refuses its control
  pipe, because it cannot inspect the elevated server's process. The pill then shows no
  speaking or listening state and its Pause and Stop do not reach the turn. The
  backend logs the reason once, as `voice control channel refused: ... most likely the voice
  server runs elevated`. Inferred from Windows' token rules and covered by a unit test; not
  yet run with a real elevated server.
- Paths: the pill's own files (the `pill` folder, barge pre-roll, interruptions, the Mac
  window position) follow `VOICEMODE_BASE_DIR`, taken from the environment, else from
  `~/.voicemode/voicemode.env`, else `~/.voicemode`. Project `.voicemode.env` files, which
  upstream finds by walking up from its working folder, are not followed: the backend has
  no session working folder. The conch, its queue and `voicemode.env` are
  read from the home folder's `.voicemode`, where upstream keeps them whatever the base dir
  (`conch.py:133`, `config.py:33`). The control socket follows upstream's `config.py:697`.
- Settings: `VOICEMODE_PILL` and `VOICEMODE_AUDIO_FEEDBACK` come from the environment first,
  then `voicemode.env`. `VOICEMODE_VOICE_BARGE` is the other way round, because the pill's
  own toggle writes it to `voicemode.env`: the file wins when it has the key, and the
  environment is only the default.
- The barge toggle runs `VOICEMODE_BARGE_CONTROL_COMMAND` with `on` or `off` appended, when
  set: a JSON list of strings or one executable path. Otherwise it runs
  `voicemode-switch barge`. That setting is read from the process environment only, never
  from `voicemode.env`, because upstream's `update_config` tool lets a model write that file.
  The voice server copies `voicemode.env` into its own environment on start and on
  `config_reload`, so the command is also refused, with one warning, whenever any env file
  upstream loads (global or project) holds the key.
- For FELIX: there the toggle changes a FELIX preference, not `voicemode.env`, so the pill
  cannot read the new state from the file. FELIX's barge control command will also need a
  `status` mode that the pill can ask.
- A restart attempt boots WSL. After `wsl --shutdown`, a running window starts it again within
  30 s. Use `voicemode-switch pill off` first.

# Voice pill: Mac window

> **TL;DR:** `patches/voice_pill_mac.py` shows the same `ui/pill` page in a frameless,
> always-on-top pywebview window, with the same backend (`voice_pill.py`) and the same
> message protocol as Windows. Tested by Robert with a real mouse through `FELIX-MAC` on the fake backend (2026-11-05,
> n=1 session per build). Real voice turns and an installed launch are untested on the Mac.

| Field | Value |
|---|---|
| Status | Released, off by default. No Mac installer yet: see the README's macOS steps |
| Measured on | Only the pure logic (`tests/test_voice_pill_mac.py`, on Linux). `FELIX-MAC` confirmed on 2026-11-04 (n=1, macOS 26.7.1, Apple silicon) that pywebview 6.2.1 with pyobjc 13.0 opens a frameless on-top window |

## Use (macOS)

```bash
voicemode-switch pill on       # makes ~/.voicemode/pill-venv (pinned; needs Python 3.10+, picks the newest found), sets the flag, opens the window
voicemode-switch pill off      # clears the flag, stops the window and backend
voicemode-switch pill status
```

Since v1.1.1 the voice server starts the window on a voice turn when `VOICEMODE_PILL=true`
and the venv exists. `voicemode-mcp` no longer does. See "Who starts and stops the window"
above. Log: `~/.voicemode/logs/pill-window.log`.
Position: `~/.voicemode/pill/window-position.txt`.

The pill venv holds pywebview 6.2.1 and the six pyobjc packages it needs, pinned to 13.0
(`PILL_MAC_PINS` in `voicemode-switch`; bottle, proxy_tools and typing_extensions come unpinned
as pywebview's own dependencies). The main venv gains nothing. The backend is stdlib only and runs under the same interpreter.

## Differences from Windows

- **Minimized goes to the tray on Windows and the menu bar on Mac.** The icon follows the
  phase, and a click restores the pill. It is not a taskbar button, because a taskbar window has
  to take focus to come back, and push-to-talk only fires while a terminal has focus. Windows 11
  may put a new tray icon in the overflow `^` until it is pinned.
- **Docked floats, on both platforms.** It sits on the screen edge at its own height, always on
  top, and takes no screen space. It starts as a slim rail (76 px: status, Stop or Wake, Pause);
  `‹` opens the full panel, and `›` there goes back to the rail. Dragging moves it up and down the edge only. `›` slides it off
  the edge, leaving a small `‹` tab at the same height; the tab cannot be dragged. Width comes
  from dragging the side edges (320 to 640, default 350), which is not available while docked. Position,
  width and dock height share one file: `x y width dock_y`, with `-` for not set.
- **Focus and Dock.** Windows blocks activation with a style bit. pywebview 6.2.1 asks for the
  Regular policy when its Cocoa module loads and activates itself when its loop starts, and with
  an Accessory switch afterwards Robert still saw a Python icon in the Dock and clicks still took
  focus (2026-11-05, n=1 session, `69303d6`). So `quiet_app()` sets `LSUIElement` in the bundle info and installs an
  `NSApplication` subclass that forces Accessory and ignores activation, before pywebview loads.
  `FELIX-MAC` measured `LSUIElement` on its own at `69303d6`: no Dock icon in 4 of 4 launches (one
  control run without it showed the icon), a small fading tile at 0.4 s in 3 of 3, and the 5 px hop gone in 3 of 3.
  `use_panel()` gives pywebview a non-activating `NSPanel` that never becomes key, and the web view
  accepts first clicks. It is shown with `orderFrontRegardless`, never pywebview's `show()`.
- **Width limits.** The window is not resizable as far as AppKit knows: its own edge resize
  once took the frame to 885x100, past the limit (FELIX-MAC, n=1). Only our edges change the width.
- **Back from the tab.** The window is sized and shown off screen in one main-thread step, then
  slides in. It used to sit at its final position for about 30 ms first, which Robert saw as a flash.
- **Hover.** With the panel never key, Robert saw no hover and no resize cursor at `909a3dc` (n=1
  session). A tracking area that works while the app is inactive reports the pointer to the page's
  `pillHover`, which sets a `.hover` class, and the wrapper sets the resize cursor at the edges.
  The edge shows a small grip on hover, on both platforms, in case the cursor is not honoured.
- **Menu bar icon.** A ball in the Windows tray's colours, with a white outline. Robert prefers
  the ball to a white symbol; the outline answers the pale teal he could not find on his blue bar.
- **Full-screen apps.** The Mac pill joins all Spaces. As a panel it also shows over full-screen
  apps (Robert, n=1), which he says does no harm.
- **Page and bridge (found by the first Mac run, 2026-11-05).** The page's CSP stays strict, so
  nothing may use `eval`. pywebview's `evaluate_js` wraps every call in `eval()` and builds its
  `js_api` functions with `new Function`, so neither works. Instead: page to Python goes through
  our own `WKScriptMessageHandler` named `pill` (`window.webkit.messageHandlers.pill.postMessage`),
  and Python to page through `WKWebView.evaluateJavaScript` called directly, on the main thread,
  with the line JSON-encoded as a string literal. The bridge script is a `WKUserScript`, which
  WebKit exempts from the page CSP. The window is created without a URL; once pywebview's blank
  page has loaded, the wrapper installs the handler and script and loads the page itself.
- **Loaded from a file, not over HTTP.** At pywebview 6.2.1 a plain path (even with
  `http_server=False`) makes `start()` open an HTTP server on `127.0.0.1:<random port>`
  (`is_local_url` in `webview/util.py`; checked in the source), which breaks "no network
  listener". Only a `file://` URL avoids it, so the wrapper passes `Path.as_uri()` and loads it
  with `loadFileURL:allowingReadAccessToURL:` (relative CSS and JS then resolve from the folder).
  Covered by a pure test; the Mac run confirms there is no listener (`lsof -i -P | grep python`).
- **Main thread only.** AppKit aborts (SIGTRAP, exit 133) on any call off the main thread. pywebview
  fires `shown` and `loaded` on worker threads, so their bodies run through `AppHelper.callAfter`,
  and reads from other threads (screen, window frame) go through `run_on_main`.
- **Files outside the repo.** WebKit caches and crash reports land under the real home directory
  whatever `HOME` is set to, so a test run with a fake `HOME` still writes there.
- **Dragging** polls `NSEvent.mouseLocation` while the left button is down, as on Windows.
- **Corners** come from the page: the window is transparent.
- **Primary screen only.** Placement and clamping use the main display. Display changes are not watched.
- **Beeps switch, both platforms:** on or off for the whole machine, next turn onward with no restart. Stored in `~/.voicemode/pill/beeps.json`; until first used it shows `VOICEMODE_AUDIO_FEEDBACK`. Off wins over an agent's `chime_enabled=true`; On does not override a caller's explicit false.
- **Barge-in stays hidden:** the backend reports `available: false` without `parec`, which macOS lacks, so the page hides it. Nothing is Mac-specific. (If someone installs PulseAudio's `parec`, it will appear; untested.)
- **Stop:** `pill off` kills the window, whose death closes the backend's stdin and ends it. A plain `kill` of the window is the same.
- **Key press:** `ptt_listener_macos.py` calls `touch_ptt()` on every hotkey press (best effort), so the two-minute rule applies here too.

## Checklist for FELIX-MAC

Setup: `git pull` on `feature/voice-pill`, run `patches/apply.sh`, `voicemode-switch pill on`.
Do not touch the local changes to `piper-proxy.py` and `voices/piper-voices.json`.
Screenshots stay on the Mac, cropped to the pill. Robert does what needs ears or macOS permissions.

Without voice, use the fake backend (`tests/pill_fake_backend.py <dir>`); run the window by hand in a terminal:
`~/.voicemode/pill-venv/bin/python patches/voice_pill_mac.py --force --backend "python3 $PWD/tests/pill_fake_backend.py /tmp/pillfb"`.
Edit `/tmp/pillfb/state.json` to change what the pill shows (for example `{"phase":"speaking"}`; also `"platform": "macos"`).
`--force` skips the VOICEMODE_PILL flag. Stop it with Ctrl-C.

Window and focus (fake backend):
- [ ] Opening the pill does not take focus from the frontmost app (read the frontmost app before and after, for example with `osascript -e 'tell application "System Events" to name of first process whose frontmost is true'`). A flicker is acceptable if it ends back on the same app; say so if you saw one
- [ ] No Dock icon, and no "python" in the menu bar
- [ ] A real click on the pill from another app: the other app stays frontmost and keeps the keyboard (type into it straight after), and the click still acts on the first press
- [ ] Voice picker: a real click opens it and a choice lands, with the pill never key. Hover colours still show
- [ ] Menu bar ball: easy to find on the bar, changes colour with the phase
- [ ] Width: the grip shows at each edge, dragging changes the width, `pill-window.log` has a `resize` line
- [ ] Clicking Pause, Stop or Wake, the dropdown and the expand button: the frontmost app does not change. **Expected to be the weak spot**: a click on any window can activate its app. If it changes, report it; the fallback is a small Swift `NSPanel` (non-activating)
- [ ] Always on top: another window dragged over it
- [ ] Above a full-screen app, and visible on a second Space (untested so far). If the window never gets that behaviour, check whether pywebview fires `shown` for a `hidden=True` window (the collection behaviour is set in that handler; look for "window behaviour not set" or its absence in `pill-window.log`)
- [ ] The page renders from `file://` (the page's CSP says `'self'`): text and buttons present, no blank window; no listening socket (`lsof -nP -iTCP -sTCP:LISTEN | grep -i python` shows nothing from the pill)
- [ ] Buttons reach the backend (Pause creates `~/.voicemode/pill/paused.json`), and snapshots reach the page (label changes with `state.json`) with the strict CSP
- [ ] Docked: floats on the edge at its own height, moves only up and down; `›` slides away to the tab, and the tab brings it back
- [ ] Side edges resize the width and it is remembered
- [ ] Transparent window with rounded corners, no square white edge; dark and light appearance
- [ ] Hide and show: snapshot `visible` false then true (`state.json`)
- [ ] Dismiss stays hidden while `visible` stays true; returns on false then true
- [ ] Modes: pill, expanded (window grows to the card height), minimized (tray on Windows, menu bar on Mac; a click brings it back), docked, tab
- [ ] Drag moves it, position survives a restart, and a position off screen is pulled back
- [ ] Disconnected when the backend dies (`echo 2 > /tmp/pillfb/exit_now`); backoff 1, 2, 4, 8, 16, 30 s in `pill-window.log`
- [ ] Exit 1 before any output (`echo 1 > /tmp/pillfb/startup_exit`): the window exits with code 1
- [ ] A second `pill on`: "already running", one window
- [ ] `pill off` closes the window and the backend, no python process left (`pgrep -fl voice_pill`)
- [ ] Closing while paused (real backend): `~/.voicemode/pill/paused.json` is gone

Backend on macOS (real `voice_pill.py`):
- [ ] A live voice session stays registered: `ps -o lstart= -p <pid>` works in `voice_pill_state.py` (check the session shows in the pill's queue after a minute, not pruned)
- [ ] A dead session is pruned (kill a session, it leaves the queue within a few seconds)
- [ ] CPU of the window processes while idle, speaking and hidden (Activity Monitor or `top`), as in the Windows table

Needs Robert (voice, ears, permissions):
- [ ] Pill appears on a real `converse`; states follow the turn
- [ ] Kokoro through `streaming.py` shows Speaking (Task 2)
- [ ] Pause, Resume and Stop work in a real turn; Wake shows the sunrise between turns and the woken session says "Listening."
- [ ] The hotkey press makes the pill appear (two-minute rule); Input Monitoring permission as for PTT
- [ ] No new stutter (`voicemode-perf`)
- [ ] Autostart at login: not provided on macOS; `voicemode-mcp` starts the window with the first session. (A LaunchAgent is not written.)

## Not tested / known limits

- Everything above, until `FELIX-MAC` runs it.
- Snapshots are sent with `evaluateJavaScript` to a window that may be ordered out; WebKit may throttle it. If the page is stale after being hidden, that is why.
- Display changes and more than one monitor.
