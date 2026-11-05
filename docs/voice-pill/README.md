# Voice pill: Windows window

> **TL;DR:** `pill-windows.ps1` shows `ui/pill` in a small always-on-top window that never
> takes focus, and runs `patches/voice_pill.py` inside WSL over stdin/stdout. Tested on the
> Windows desktop with a fake backend and the real backend under a fake HOME. Not yet tested
> in a real voice turn.

| Field | Value |
|---|---|
| Status | In progress: the real voice checks below are open |
| Last updated | 2026-11-05 |
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

`pill on` waits up to 15 s and fails with exit 1 if the window exited with an error. It cannot
detect a failure after those 15 s. "Already running" only means a window holds the lock: that
window may have no backend. `pill off` then `pill on` starts clean.

Files are in `%LOCALAPPDATA%\voicemode-local`: `webview2\` (two DLLs), `webview2-data\`,
`ui\` (copied at each start), `pill-position.txt` and `pill-window.log`.

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
- **Stopping** needs no backend: the window waits on a `Local\VoiceModePillStop` event, which
  `pill off` sets through `powershell.exe`. The flag is also read when a backend exits and
  before each retry.

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
| Dock reserves the edge | Work area `0,0,1600,1032` right, `320,0,1920,1032` left |
| Work area restored | On undock and on hide (n=1 each), on close and on hard kill (n=2 each) |

CPU, all window processes together over 20 s on 20 logical CPUs (FB, n=1 unless stated):

| State | % of machine | % of one core |
|---|---|---|
| Visible, idle | 0.10, 0.12 (n=2) | 2.0, 2.3 |
| Docked, idle | 0.05 | 1.1 |
| Hidden | 0.12 | 2.4 |
| Listening | 0.09 | 1.7 |
| Speaking | 0.11 | 2.2 |

The orb first had a pulse animation, removed on 2026-11-05 because audio runs during those
states: listening cost 10.4 and 17.1% of one core with it (n=2), and speaking 14.8% (n=1). State
now shows by colour only. Whether the old or new cost affects stutter was not measured.

## Open: needs Robert's voice

- [ ] Pill appears on a real `converse`
- [ ] States change correctly in a real turn
- [ ] Interrupt, Pause, Resume and Stop work in a real turn
- [ ] Closing the window while paused releases Pause in a real turn
- [ ] No new stutter under a voice turn (`voicemode-perf`)

## Not tested

- `wsl --terminate` and recovery: it would have ended every WSL session on this machine.
- A real login through the Startup folder. The generated `.cmd` was run from a temp folder.
- More than one monitor, a monitor being unplugged, and scales other than 100%.
- Fullscreen apps: the pill stays on top of them, and nothing handles that yet.
- Explorer restarting while docked.

## Known limits

- An unhandled Python exception before the first snapshot also exits 1, so it is not retried.
  It appears in `pill-window.log`. Run `pill off`, then `pill on`, to restart.
- A restart attempt boots WSL. After `wsl --shutdown`, a running window starts it again within
  30 s. Use `voicemode-switch pill off` first.
- Docked, the window takes 320 px of screen width at full height.

# Voice pill: Mac window

> **TL;DR:** `patches/voice_pill_mac.py` shows the same `ui/pill` page in a frameless,
> always-on-top pywebview window, with the same backend (`voice_pill.py`) and the same
> message protocol as Windows. Written on Linux; **nothing in the GUI part has run yet**. The
> checklist below is for `FELIX-MAC`.

| Field | Value |
|---|---|
| Status | Implemented, untested on the Mac |
| Measured on | Only the pure logic (`tests/test_voice_pill_mac.py`, on Linux). `FELIX-MAC` confirmed on 2026-11-04 (n=1, macOS 26.7.1, Apple silicon) that pywebview 6.2.1 with pyobjc 13.0 opens a frameless on-top window |

## Use (macOS)

```bash
voicemode-switch pill on       # makes ~/.voicemode/pill-venv (pinned; needs Python 3.10+, picks the newest found), sets the flag, opens the window
voicemode-switch pill off      # clears the flag, stops the window and backend
voicemode-switch pill status
```

`voicemode-mcp` also starts one window per machine when `VOICEMODE_PILL=true` and the venv
exists (a launch lock like the PTT listener's, and `pgrep` instead of a pidfile, since a launch that loses the window's own lock exits at once). Log: `~/.voicemode/logs/pill-window.log`.
Position: `~/.voicemode/pill/window-position.txt`.

The pill venv holds pywebview 6.2.1 and the six pyobjc packages it needs, pinned to 13.0
(`PILL_MAC_PINS` in `voicemode-switch`; bottle, proxy_tools and typing_extensions come unpinned
as pywebview's own dependencies). The main venv gains nothing. The backend is stdlib only and runs under the same interpreter.

## Differences from Windows

- **Docked is not a reserved area.** macOS has no AppBar. "Docked" snaps the window to the
  left or right edge of the usable screen (below the menu bar, above the Dock) at full height,
  and other windows can still cover that strip. Pushing windows aside would need Accessibility
  permission and fighting every app, so it is not attempted.
- **Focus.** Windows blocks activation with a style bit. Here the activation policy is
  Accessory (no Dock icon, no menu bar), set after pywebview's import because that resets it
  to Regular, and set again in the `shown` event. pywebview activates the app once when its loop
  starts: the previously frontmost app is re-activated at once. The window is shown with
  `orderFrontRegardless`, never pywebview's `show()`, which activates.
- **Dragging** polls `NSEvent.mouseLocation` while the left button is down, as on Windows.
- **Corners** come from the page: the window is transparent.
- **Primary screen only.** Placement and clamping use the main display. Display changes are not watched.
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
- [ ] Clicking Pause, Interrupt, the dropdown and the expand button: the frontmost app does not change. **Expected to be the weak spot**: a click on any window can activate its app. If it changes, report it; the fallback is a small Swift `NSPanel` (non-activating)
- [ ] Always on top: another window dragged over it
- [ ] Above a full-screen app, and visible on a second Space (untested so far). If the window never gets that behaviour, check whether pywebview fires `shown` for a `hidden=True` window (the collection behaviour is set in that handler; look for "window behaviour not set" or its absence in `pill-window.log`)
- [ ] The page renders from `file://` (the page's CSP says `'self'`): text and buttons present, no blank window
- [ ] Transparent window with rounded corners, no square white edge; dark and light appearance
- [ ] Hide and show: snapshot `visible` false then true (`state.json`)
- [ ] Dismiss stays hidden while `visible` stays true; returns on false then true
- [ ] Modes: pill, expanded (window grows to the card height), dot (16 px), docked left and right at full height
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
- [ ] Interrupt, Pause, Resume and Stop work in a real turn
- [ ] The hotkey press makes the pill appear (two-minute rule); Input Monitoring permission as for PTT
- [ ] No new stutter (`voicemode-perf`)
- [ ] Autostart at login: not provided on macOS; `voicemode-mcp` starts the window with the first session. (A LaunchAgent is not written.)

## Not tested / known limits

- Everything above, until `FELIX-MAC` runs it.
- `evaluate_js` to a window that is ordered out may stall the reader thread. If snapshots stop arriving while hidden, that is why.
- Display changes and more than one monitor.
