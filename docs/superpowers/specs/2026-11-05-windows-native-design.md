# Native Windows, native barge-in, and delivery to FEs through FELIX

Status: draft, 2026-11-05. Written by a read-only design agent; locations read in the code
unless marked (I) for inferred. Not yet reviewed by FELIX-voice or approved by Robert.

## Goal (Robert, 2026-11-05)

- voicemode-local runs natively on Windows, without WSL, including the pill.
- Voice barge-in works natively on Windows and on macOS, not only Linux and WSL.
- FEs get barge-in, the pill on Windows and the pill on Mac through FELIX.

## Facts it rests on

- Windows Python has no `socket.AF_UNIX` (checked on 3.14.7 here and 3.12.10 in FELIX's
  runtime), nor `os.getuid` or `fcntl`. Upstream's control channel uses AF_UNIX
  (`control_socket.py:317`), so Stop, Interrupt, Pause, push-to-talk and the pill's speaking
  status have no route natively.
- FELIX keeps the control channel off on every OS (`voice.py:237` on `feat/voice-opt-in`).
- A native converse ran with FELIX's patches: reported, n=1 conversation, two turns. Its
  scipy patch fixed a hang with the same symptom as `docs/windows-issues.md` issue 5;
  before and after n=1, cause link unproven.
- `os.kill(pid, 0)` terminates the process on Windows: from code comments, not run.
- FELIX's constraints: scratchpad `felix-constraints.md`, reported by FELIX-voice.

## 1. Control transport: a named pipe

Server `control_socket.py:317`, threaded (`:253`, `:414`). Clients `control_socket.py:643`,
`:684`, `voice_pill_state.py:363`, `ptt_control_client.py:68`, `:135`.

Chosen over loopback with a token: peer credentials are unavailable on Windows
(`control_socket.py:110`, `:399`); a pipe's DACL is enforced at open, so the wire protocol is
unchanged; a token changes every client and its file needs the same ACL work. Loopback's one
advantage is needing no shim.

Owner-only: `CreateNamedPipeW` through ctypes (the stdlib passes NULL security,
`multiprocessing/connection.py:680`); DACL `D:P(A;;GA;;;<user SID>)`;
`FILE_FLAG_FIRST_PIPE_INSTANCE`; `PIPE_REJECT_REMOTE_CLIENTS` (absent from `_winapi`); clients
check the pipe's owner is their SID. Name `\\.\pipe\voicemode-control-<hash of control.sock
path>`, so each install's base dir gets its own pipe.

Bugs found by reading: with the flag on natively, `_bind` raises AttributeError, only OSError
is caught (`control_socket.py:573`), and the call is outside the `try` (`converse.py:1712`), so
a turn would be lost (I). `ptt_control_client.py:56` reads `VOICEMODE_CONTROL_SOCKET_PATH`;
upstream reads `VOICEMODE_CONTROL_SOCKET` (`config.py:697`).

## 2. Unix assumptions to replace

| Area | Where | Replacement |
|---|---|---|
| Liveness | `voice_pill_state.py:61` (`ps`); `:157`, `:202` then delete live sessions and Pause | ctypes `OpenProcess` + `GetProcessTimes` |
| Lock | `voice_pill.py:39`, `:304` | `msvcrt` far-offset byte (`file_lock.py:34`) |
| Stdin | `voice_pill.py:332` (`select`) | reader thread |
| Signals | `voice_pill.py:358`, `voice_barge.py:325`; `pill-windows.ps1:570` kills only the venv launcher | EOF, then a Job Object |
| Permissions | `voice_pill_state.py:37`, `:298`; `voice_barge.py:168` | directory ACL |
| Atomic read/write | `voice_pill_state.py:82`, `:97`, then `:157` unlinks | retry; never unlink after a failed read |
| Encoding | `voice_pill.py:190`, `:205`; patch `read_text` | explicit UTF-8, tolerate CRLF |
| Fixed `~/.voicemode` | `voice_pill_state.py:29`, `:359`; `voice_pill.py:57`; `voice_barge.py:161`, `:296`; `ptt_control_client.py:49`; `patch_session_name.py:110` | one resolver honouring `VOICEMODE_BASE_DIR` (`config.py:545`) |
| Settings | `voice_pill.py:190`, `:205`, `voice_barge.py:296` read only `voicemode.env` | process environment first |
| Switch lookup | `voice_pill_state.py:403`, `:418` (bash `voicemode-switch`) | configurable control command |

## 3. Window

In `pill-windows.ps1`, a `\\wsl` path or `-Distro` keeps today's WSL path; otherwise run
native (that case throws today at `:122`, so nothing regresses). Replace `:268` and `:200`
with an exe/args pair and a direct env read. Find Python through an `active.json` like FELIX's
`launch.js:40`, never `python3`. A Python launcher, a port of `voicemode-mcp`, starts services,
pill, listener and voice-mode (FELIX's `connector_options`, `voice.py:694`).

## 4. Push-to-talk

`ptt-listener-windows.ps1` sends to the pipe with `NamedPipeClientStream`, using
`ptt_control_client.py:124`'s mapping; drop the `press` probe (`:137`); parameterise the log
path (`:51`). The relay stays WSL-only.

## 5. Install

`install.ps1`'s `windows-native` mode today requires PowerShell 7 and Docker, copies four files
that no longer exist (skipped silently, so no patch runs), and logs "updated" for a regex that
cannot match 8.12. `C:\git\voicemode-local` is at 9d898e6 (voice-mode 8.7.1), 120 commits
behind, clean.

`patches/apply.py` with a manifest becomes authoritative; `apply.sh` delegates once output is
byte-identical. Take from FELIX: hash-locked uv sync (`voice.py:400`), atomic runtime selection
(`:391`), the Whisper zip (`:310`), model hard links (`:326`), leases and watchdog
(`:711-807`), `kill_tree`, `on_path`. `kokoro-onnx-server.py:273` and `whisper-proxy.py:213`
bind 0.0.0.0, which breaks "nothing reachable from outside" natively: add `--host`.

## 6. Barge-in, native

`SoundDeviceCapture`: a sounddevice `InputStream`, 16 kHz mono int16, with the same
`read_window()` and `close()` as `voice_barge.py:119-153`, so `run()` and its tests stay.
Serves Windows and macOS; Linux and WSL keep `parec`. The device setting becomes a sounddevice
name or index.

Handoff and pre-roll logic is portable; its path comes from the resolver; `os.replace` can
race the read-then-unlink on Windows and needs a retry (I); the folder ACL protects the file.

`onnxruntime`, `numpy` and `sounddevice` are already in FELIX's lock; the Silero model is
pinned by URL and SHA-256, and FELIX also needs its size (not measured).

Mic open during playback (all I): separate processes; shared-mode WASAPI and MME should allow
it, exclusive mode would not. Risks: Bluetooth headsets switching to hands-free; the trigger's
stream overlapping the server's recording during the handoff on the device that showed the
hang; built-in echo cancellation shifting every echo level.

Single instance: today `voicemode-switch:362` uses a pid file with no lock. Natively, a leased
service with an `msvcrt`/`fcntl` lock in the base dir, stopped by the watchdog.

Redo per device every measurement in `docs/voice-barge-in/README.md`: the -40 dBFS gate, the
192 ms minimum, echo level on speakers, false triggers while silent, words kept, cut latency.
Laptop speakers, a USB headset on MME, and a Mac's built-in speakers and mic, with and without
load.

## 7. FELIX

- FELIX carries no hold patches; barge anchors on them and the pill anchors on barge.
  Re-anchor both on pristine 8.12 and test three trees: pristine, pristine plus FELIX's
  patches, and the full voicemode-local order.
- FELIX overrides HOME, USERPROFILE and the base dir, so every fixed `~/.voicemode` breaks
  until the resolver exists. The pill uses no speech ports.
- `barge_control` finds no `voicemode-switch` there: FELIX needs its own barge command,
  passed in through a setting.
- Two installs on one machine: control paths stay separate by hash; each install would run
  its own trigger on the same mic; the pill mutex `Local\VoiceModePillWindow` admits one
  window showing one install's sessions.
- voicemode-local should be vendorable unchanged: no fixed paths, environment before file,
  every patch runnable as `python patch.py target` and CRLF-safe, no dependency on
  `voicemode-switch`, start hooks passed in.

| Feature | FELIX changes |
|---|---|
| All | vendor the transport, pill, barge and patches; list them in dependencies.json; control channel on only when pill or barge is on |
| Barge | Silero pinned with size; trigger as a leased service |
| Windows pill | vendor the window and page; pin the WebView2 nupkg; start through FELIX's detached start |
| Mac pill | pywebview and pyobjc in a separate hash-locked venv, keeping the main runtime digest |

## 8. Plan

| # | Task | Linux proves | Needs Windows or Mac | Risk to Mac/WSL |
|---|---|---|---|---|
| 1 | Pipe transport and patch (first slice) | anchors, POSIX unchanged | real pipe: DACL, EOF, timeouts; no audio | client env fix |
| 2 | Resolver, env-first settings, backend portability | unit tests | backend with fake sessions | stdio loop |
| 3 | Re-anchor patches, `apply.py` | three patch trees | Windows venv | high: release path |
| 4 | Native runtime, services, launcher, `--host` | unit tests | real converse | Kokoro default |
| 5 | Barge capture and trigger lifecycle | fake capture | mic during playback, measurements | shared trigger |
| 6 | Window native mode | static checks | no-focus, WSL regression | WSL pill |
| 7 | Push-to-talk native mode | name vectors | real keys | WSL default |
| 8 | Native switch and install | little | clean machine, no admin | `install.ps1` |

FELIX phase after #1603 lands: F1 vendor bump and patch list; F2 control channel setting and
barge; F3 Windows pill; F4 Mac pill venv. Tasks 1-8 do not wait on #1603.

## 9. Open decisions (Robert; answered in section 11)

1. One pill for both installs on a machine, or one per install?
2. Barge per install, and may two triggers share one mic?
3. Separate FE opt-in questions for the pill and barge, given new downloads?
4. Bluetooth headsets in scope for barge?
5. German voices natively?
6. Pull `C:\git\voicemode-local`, or install natively elsewhere?

## 10. FELIX-voice review, 2026-11-05 (reported; to apply in the tasks)

Line numbers in voice.py checked by FELIX-voice on aea841ffd. Direction right.

Wrong:
1. Pill and barge trigger start with the speech services from the connector (ensure_services), leased, stopped by the watchdog; not FELIX's detached WMI start, which is only for the install worker.
2. FELIX overrides HOME, USERPROFILE, base dir, not LOCALAPPDATA. The Windows pill's %LOCALAPPDATA%\voicemode-local (DLLs, webview2-data, ui, position, log) would be shared between installs: move under the resolver's base dir or make it a setting.
3. In FELIX the control.sock path changes on every runtime rebuild: never cache the pipe name; same for locks and the pill mutex. A fixed Local\VoiceModePillWindow lets one install's pill block the other's. Decision 1 needed before FELIX ships.

Missing:
4. A window started from a hidden process (launch.js windowsHide, CREATE_NO_WINDOW at voice.py:161, :701, :707): whether WPF/WebView2 shows and keeps no-focus is unmeasured. Test under the real app.
5. Order: the pipe patch must be vendored before FELIX ever turns the channel on. Add a test that fails if the channel is on without the transport patch on Windows.
6. FE settings: pill and barge on/off become FELIX preferences next to voice.local (fe-bootstrap/preferences/registry.json:79), read by voice.py; questions in #1608's approvals shape; the voice_barge MCP tool must change the preference, not a runtime file.
7. Repair must check the Mac pill venv, Silero model and WebView2 DLLs; measure and state every size; low-disk check includes them; nuget.org joins the off-network hosts.
8. Mac mic ownership: measure which app macOS asks permission for when the trigger (child of the Copilot app) opens the mic, and two processes opening the mic at once on a Mac.
9. Fourth test tree: FELIX's patches (no_ffmpeg_gate, no_ffmpeg_warning, load_scipy_early) then barge and pill, in dependencies.json order.
10. Stopping: kill_tree, leases, voice.py stop; on off they stop with the services and leave a personal install's trigger running. A Job Object belongs to FELIX's service process, not the pill.

Minor: FELIX's kokoro copy already binds loopback. F1 re-anchoring rebuilds every runtime once; on #1603 the next chat repairs it.

## 11. Robert's decisions, 2026-11-05

1. One pill per install. 2. Barge per install, one trigger per mic. 4. Bluetooth headsets tested, not promised. 5. German later, on request. 6. Leave C:\git\voicemode-local; install natively elsewhere. 3 (FE questions and downloads) belongs to a FELIX session. Measured here: Silero model 2,327,524 bytes; WebView2 nupkg 9,818,548 bytes download, 906,432 kept.
