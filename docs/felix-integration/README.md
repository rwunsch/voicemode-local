# voicemode-local in FELIX: what to vendor and how to wire it

> **TL;DR:** pin a voicemode-local release tag, apply its patch list into FELIX's runtime, pin two
> extra downloads (Silero, WebView2), start the pill and the barge trigger with FELIX's speech
> services, and pass four settings in the environment. FELIX decides the questions to FEs and
> where the switches live. This is information for FELIX, not instructions.

| Field        | Value |
|--------------|-------|
| Status       | DRAFT, for v1.1.0 (not tagged yet; sent to FELIX only once tagged) |
| Created      | 2026-11-06 |
| Last Updated | 2026-11-06 |
| Project      | voicemode-local, branch feature/windows-native |
| Order        | Robert, 2026-11-06: v1.1.0 goes to FELIX stage only after felix-framework #1603 has landed |

Evidence tiers: **validated** = run by the voicemode session itself, with n; **reported** = run by
another session (FELIX-MAC, FELIX-voice) and not re-run here; **inferred** = read in code, not run.

## What v1.1.0 adds over v1.0.0

| Feature | Linux/WSL | Windows (native) | macOS |
|---|---|---|---|
| Voice barge-in (talk over the assistant to stop it) | parec | sounddevice | sounddevice on headphones; macOS voice processing (echo cancellation) on the built-in speakers |
| Voice pill (status window) | WSLg via pill-windows.ps1 | pill-windows.ps1 native mode, one per install | pywebview |
| Native install without WSL/Docker/admin | n/a | install.ps1 -Mode windows-native | install-mac.sh (v1.0.0) + `--barge` |
| Live on/off switch | voicemode-switch | voicemode_native.py barge/pill on/off/status | voicemode-switch |

## What v1.2.0 changes (not tagged yet)

On `feature/shared-supervisor`: not merged, not tagged. The shared voice supervisor, `vml_supervisor/`,
replaces the lifecycle code that voicemode-local's native launcher and FELIX's `voice.py` each carried.

- **The shared supervisor and the extension pattern.** A core, a profile (plain data) and named
  hooks. Fields, entry points, every Result code and the stability rule are in
  `vml_supervisor/README.md`; the hook contract is the docstring of `vml_supervisor/hooks.py`.
  `tests/test_supervisor_felix_profile.py` shows a FELIX-shaped profile (its ports, markers,
  `ports.json` override and example hooks) running the service lifecycle.
- **What FELIX vendors:** `vml_supervisor/*`, byte-identical at the tag, never patched locally.
- **What FELIX and native users will notice:**
  - On Windows the host runs outside every chat, so services and helpers survive a chat closing.
  - `host-profile.json` (mode 0600) holds the environment the host hands to its processes.
  - `run/ports.json` (the chosen ports) and `run/modes.json` (the latest helper modes) are new state files.
  - A `read_mode` of "off" stops a running helper instead of leaving it.
  - `host_requires_psutil`: on Windows a host is not started without `psutil`.
  - `serve` on Windows may get its helpers from the host rather than start them itself.
  - An in-place upgrade restarts old helpers once: their records lack a mode.
- **What remains:** FELIX converts `voice.py` into a thin profile in its own PR. The tag waits for Robert's go.

## What v1.1.3 changes (tagged 2026-11-12, merged to main from fix/talos-review; fixes from Talos's review of FELIX #1743)

27 findings on vendored files were checked against the code (all confirmed or partly), then
fixed test-first, then reviewed again by a fresh reviewer. Highlights, by what FELIX notices:

- **Security: voicemode.env can no longer inject programs or environment.** Upstream's
  `update_config` tool writes any key, and the server copied the file into its environment.
  - Only voice settings travel from voicemode.env into the environment now: `VOICEMODE_*`,
    `OPENAI_API_KEY`, `CARTESIA_API_KEY`. `update_config` refuses any other key
    (`patch_env_allowlist.py`, new: targets `config.py` and `tools/configuration_management.py`,
    both added to `unshare`).
  - `VOICEMODE_CONCH_WAKE_COMMAND` and `VOICEMODE_PILL_PYTHON` are trusted from the real
    environment only, like the barge command. **FELIX must pass them in the process
    environment**, never through voicemode.env.
- **Pill.**
  - Stop works while paused.
  - The "auto" barge notice says it listens on headphones only.
  - German is hidden when only English Kokoro is installed. **Set
    `VOICEMODE_PILL_LANGUAGES=English` to be certain**: the model folder check assumes the
    runtime layout and has not been seen on a FELIX machine.
  - Push-to-talk keys show only where a listener runs. FELIX on a Mac shows none unless it sets
    `VOICEMODE_PTT_ENABLED=true`.
  - Saved voice and language choices are dropped once at the upgrade.
- **Mac pill:** runs on `VOICEMODE_PILL` from the environment (it used to exit on every FELIX
  launch), and a watchdog restart keeps the user's pause.
- **Windows pill (`pill-windows.ps1`):**
  - No startup hang on a stalled WSL lookup.
  - `"true"`, `export` and any case of `true` are read as on.
  - The autostart entry keeps its arguments.
  - A command that cannot reach a restarting backend marks the page disconnected at once.
- **Barge trigger:** survives a failed preroll save and a failed mic open, and a pause right
  after a barge drops the captured audio.
- **Push-to-talk:**
  - The WSL relay requires a token (`~/.voicemode/ptt-relay.token`). FELIX's native sender
    mode does not use the relay.
  - A tap behind a slow send stays a tap.
  - Linux and Mac listeners release a hold when stopped.
- **Not run on real Windows or Mac hardware:** tests ran on WSL, partly through
  `powershell.exe`.

## What v1.1.2 changes (tagged 2026-11-11, merged to main from fix/conch-minors)

Small fixes from the v1.1.1 final review. No new settings and no manifest changes. Files:
`patches/conch_wakers.py`, `patches/patch_conch_wakers.py`, `patches/patch_conch_fairness.py`
and `patches/patch_session_name.py`.

- **Wake counts** (`conch.wakes.json`) are locked across processes, so concurrent pings
  can no longer exceed the 5-wake cap. Entries older than a day are dropped. On Windows,
  a refused replace is retried.
- **A free mic reserved for a queued turn is named.** converse says "<name> (reserved for
  its queued turn; nobody is speaking yet)" instead of "unknown holds the voice channel".
- **Copilot chat titles are read as YAML** (`yaml.safe_load`, a voice-mode dependency):
  block scalars and escaped quotes work. If the file does not parse, the old line reader
  is the fallback.
- **An older patch text is reported.** A file with the conch marker but an older text
  prints a stderr line saying it is kept and that a reinstall gives the new text.
  It still exits 0.

## What v1.1.1 changes (tagged 2026-11-11, merged to main from feature/pill-autostart)

Agreed with FELIX-voice on 2026-11-10. Robert's decisions are in
`docs/superpowers/plans/2026-11-10-pill-autostart.md`.

- **The voice server starts the pill itself.** Section 4's pill line is replaced: FELIX no
  longer starts or stops a pill window.
  - It opens on the first voice turn and closes (backend exit code 3) about 20 s after the
    last voice session ends.
  - A ✕ close stays closed until voice starts again from scratch.
  - See `docs/voice-pill/README.md`, "Who starts and stops the window".
- **New settings** (environment first):

  | Setting | FELIX value |
  |---|---|
  | `VOICEMODE_PILL` | `true` to have a pill |
  | `VOICEMODE_PILL_AUTOSTART` | leave unset (`off` = the server never starts a window) |
  | `VOICEMODE_PILL_DATA_DIR` | FELIX's pill window folder, e.g. `<voice root>\pill-window`, with WebView2 pre-installed |
  | `VOICEMODE_PILL_PYTHON` | macOS: the pill venv's python. Windows: optional, default is the server's own interpreter. **Environment only** since v1.1.3: a value in a voicemode.env is ignored |
  | `VOICEMODE_CONTROL_CHANNEL_ENABLED` | still `true` for live state |

- **Barge `auto`.** `VOICEMODE_VOICE_BARGE=auto` listens only while output goes to headphones.
  `VOICEMODE_BARGE_CONTROL_COMMAND` may now receive `auto` as its appended action, besides
  `on`, `off` and `status`.
- **Conch callback (found in FELIX Windows test 3, 2026-11-11).**
  - **What happened:** a chat queued with `conch_mode="callback"`. Upstream wakes such a
    chat only through tmux, and never expires its grant, so the queue stood still.
  - **Fix:** `conch_wakers.py` + `patch_conch_wakers.py`.
    - Callback is served as wait wherever no waker can reach the chat, which is FELIX today.
    - Where a waker exists, the granted chat is woken. The wakers, in order: the
      `VOICEMODE_CONCH_WAKE_COMMAND` environment setting (a JSON list, run with the
      session id and voice server pid appended; exit 0 = delivered, exit 75 = busy, it
      keeps its place), then Claude Code's session socket (POSIX), then tmux.
    - A woken chat has 10 s (`VOICEMODE_CONCH_CALLBACK_GRACE`) to come back. After that,
      a session that is present may speak past it; the absent chat keeps its place and is
      woken again at the next release, at most 5 times per queued turn
      (`VOICEMODE_CONCH_MAX_WAKES`). These clocks never run while someone holds the mic.
    - Validated live on WSL with two Claude Code sessions, 2026-11-11 (n=1 each): two
      delivered wakes, two busy skips with the place kept, one pass of an absent grant.
  - **For FELIX later: the Copilot wake path.** No standard MCP message starts a model
    turn (spike, reported). An in-chat extension's `session.send` does wake an idle chat
    (reported: idle 1.5 min woken in +7.4 s, idle 12 min 18 s woken in +14 s, n=1 each).
    A send to a busy chat queues and never reports busy. So the bridge behind
    `VOICEMODE_CONCH_WAKE_COMMAND` must return 75 itself, using the extension's
    `session.idle` events, and keep at most one pending wake per chat. FELIX builds it.
  - **Only a grant that names a chat wakes it.** Upstream's ping to a callback waiter
    that it skips no longer wakes anyone, because our wake text says "your turn has
    come". A spent `conch give` becomes an ordinary grant.
  - Patch targets: `tools/converse.py`, `conch_notify.py`, `conch_queue.py`, `conch.py`.
    The manifests add `conch_notify.py`, `conch_queue.py` and `conch.py` to `unshare`.
- **Fair holds (`conch_hold.py`, `patch_conch_fairness.py`).** While another chat is
  blocked waiting (wait mode), a `hold_conch` turn with no reply releases the mic, and at
  most 3 held turns in a row keep it (`VOICEMODE_CONCH_HOLD_MAX_TURNS`). A speak-only
  held turn counts toward the 3. A busy callback chat does not count as waiting.
  Validated on WSL with Claude Code (no-reply release n=3, the 3-turn cap n=1).
- **MCP progress heartbeat (`mcp_progress.py`).** Copilot CLI cancels a tool call after
  180 s unless progress arrives (reported, CLI 1.0.107). converse now sends progress every
  30 s (`VOICEMODE_PROGRESS_INTERVAL`, 0 = off), but only when the client sent a progress
  token. Each call logs "progress heartbeat: … client token present/absent"; with
  `VOICEMODE_DEBUG=true` that line is in `%USERPROFILE%\voicemode_debug.log`. Claude
  Code: token present (validated, n=3). Copilot: unmeasured. Keep `"timeout": 960000`
  on the connector either way. A cancelled conch wait now logs
  `CONCH_WAIT_END result=cancelled`.
- **Push-to-talk focus (`ptt-listener-windows.ps1 -FocusPattern`).** The default is
  unchanged (terminals and VS Code). FELIX passes `'^github$'` for the Copilot app.
- **Manifests copy files from outside `patches/`.** The copy sources are `../pill-windows.ps1`
  and `../ui/pill/*`, copied to `pill-windows.ps1` and `ui/pill/*` in the package.
  `apply.py` now creates the parent folders. A vendored copy needs those files beside
  `patches/`.
- **Open for FELIX.**
  - Whether the Copilot app's Job Object allows breakaway (FELIX-voice is measuring). If it
    doesn't, the window lives as long as the chat that started it, and the next turn of
    another chat restarts it.
  - Native Windows and macOS live runs are still to do. WSL is validated (n=1 per step).

## 1. Patches

- `patches/manifest.json`: the list for WSL/Linux/macOS (21 steps at 7c81771). Its output is kept
  byte-identical release to release except for the copied modules, and tests pin that.
- `patches/manifest.windows.json`: the native Windows list (26 steps): the same list plus FELIX's
  `patch_no_ffmpeg_gate.py`, `patch_no_ffmpeg_warning.py`, `patch_load_scipy_early.py` (copied from
  felix-framework aea841ffd, line endings kept), `patch_windows_tempfile.py` (WinError 32 on the buffered
  TTS path) and `patch_native_client_folder.py`.
- Apply with `python patches/apply.py [--native-windows] <site-packages/voice_mode>`. Every patch is
  idempotent, CRLF-safe and stops on a missing anchor. Anchored on pristine voice-mode 8.12.0.
- Order matters: `patch_control_transport.py` (the named-pipe control channel on Windows) must be applied before
  FELIX ever turns the control channel on; a test in this repo fails if the channel is on natively without it.

## 2. Downloads to pin (validated sizes and hashes)

| File | Bytes | SHA-256 | Needed for |
|---|---|---|---|
| `silero_vad.onnx` (snakers4/silero-vad @ bfdc0193) | 2,327,524 | 1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3 | barge, all OSes |
| `microsoft.web.webview2.1.0.4314.35.nupkg` (nuget.org) | 9,818,548 download, 906,432 kept | 90778DEB1B77E36292828B4B5833C9255C27533F40E0DDD55891AE718944EDC4 | Windows pill |
| `pyobjc-framework-AVFoundation` (+ core, CoreAudio, CoreMedia, Cocoa, Quartz), same release as the venv's pyobjc-core (12.2.2 on Robert's Mac) | +1.3 MB measured on the Mac (reported) | pinned by version | macOS barge on speakers |

Repair (FELIX point 7) should check all three, and nuget.org joins the hosts that must be reachable.

## 3. Settings FELIX must pass (environment wins over voicemode.env)

| Setting | Value when the feature is on | Why |
|---|---|---|
| `VOICEMODE_CONTROL_CHANNEL_ENABLED` | `true` when pill, barge or push-to-talk is on | barge and the pill talk to voice-mode over it. Without it barge never fires: native Windows 0 cuts without, 2/2 with (validated). FELIX's own servers set it to `false` in their environment today (reported by FELIX-MAC), which wins over any file |
| `VOICEMODE_VOICE_BARGE` | `true` / `false` | the trigger runs only when true |
| `VOICEMODE_BARGE_CONTROL_COMMAND` | JSON list, e.g. `["<python>", "<FELIX's switch>", "barge"]` | what the `voice_barge` MCP tool and the pill's toggle run. Read from the ENVIRONMENT only: refused if any voicemode.env holds it, because a model can write that file |
| `VOICEMODE_BARGE_PYTHON` | the trigger's interpreter | the pill asks it whether barge can run, when the pill's own Python cannot hear the mic (macOS pill venv) |
| `VOICEMODE_TOOLS_ENABLED` | include `voice_barge_tool` if FELIX uses a tool list | voice-mode 8.12 loads only converse and service without a list; our native launcher adds it when no list is set |
| `VOICEMODE_STREAMING_ENABLED` | our native launcher sets `false` with barge unless the user set it | the README once said barge is inert with streaming; on macOS it cut playback 3 times with streaming on (reported). Not re-tested on Windows with streaming on |
| `VOICEMODE_SESSION_WAKE` | `off` for FELIX | default `on`. Push-to-talk holds with no voice turn and the pill's Play wake an idle Claude Code session through Claude Code's undocumented session socket (`patches/session_wake.py`). See section 6 |
| `VOICEMODE_VOICE_BARGE_ECHO` | `auto` (default), `on`, `off` | macOS only: whether speakers use voice processing |

## 4. Starting and stopping (FELIX point 1 and 10)

- Pill and barge start with FELIX's speech services, under its leases and watchdog, and stop with them.
  Not detached.
- Barge: `<runtime python> -m voice_mode.voice_barge <path to silero_vad.onnx>`. One per install.
- Windows pill: `powershell.exe -STA -NoProfile -ExecutionPolicy Bypass -File pill-windows.ps1 -BackendExe <runtime python>
  -BackendArgs "-m voice_mode.voice_pill --stdio" -DataDir <FELIX root>\pill`. `-DataDir` keeps FELIX's pill
  files, mutex and stop event apart from a personal install (names are derived from a hash of the normalised
  folder; validated that PowerShell and Python agree on 10 spellings of one folder).
- macOS pill: pywebview + pyobjc in a separate hash-locked venv (FELIX F4).
- State for FELIX at voice start: `python -m voice_mode.voice_barge status` prints `on`, `off` or
  `cannot run: <reason>`, exit 0. On macOS a second line `route: <speakers|headphones|unknown>, capture: <…>`.

## 5. What works where (measured 2026-11-06)

| Setup | Silent turns cut? | Talk-over cut? | Tier |
|---|---|---|---|
| Windows, Jabra headset | — | 2/2, at -17/-20 dBFS, words kept | validated |
| Windows, laptop speakers + Intel mic array | 0/2 | 1/1 at -35.4 dBFS | validated |
| Windows, laptop speakers + Aukey USB mic (no driver processing) | **3/3 cut itself off** | — | validated |
| macOS, built-in speakers, plain capture | **2/2 cut itself off** | — | reported |
| macOS, built-in speakers, voice processing | 0/4 | 1/1 at -18.4 dBFS | reported |
| macOS, Bluetooth headset | 0/2 | 2/2 | reported |

Rule: headsets work everywhere; speakers need an echo-cancelled mic. On Windows that is today only a mic
whose driver cancels echo (the Intel array did; inferred mechanism). On Windows with another mic on speakers,
barge will cut itself off: a Windows echo-cancelled capture is planned, not built. Barge stays off by default.
Every stretch logs one line (`stretch: s, windows, max, p90, fired, …`) for diagnosis.

## 6. Known gaps

- **One queue per voice-mode home.** Chats share a queue only if they share
  `VOICEMODE_BASE_DIR`. A WSL voicemode-local install never shares one with FELIX on
  Windows (see `docs/windows-native/README.md`, Gotchas). FELIX itself split its queue
  across runtime rebuilds; FELIX-voice reported this on 2026-11-11 and is fixing it in
  FELIX #1743 with one stable home across runtimes.

- **Session wake: FELIX should not use it** (set `VOICEMODE_SESSION_WAKE=off`).
  `patches/session_wake.py` messages an idle Claude Code session over Claude Code's internal
  peer socket. That protocol is undocumented (read from the binary's strings), validated
  once (n=1, WSL, 2.1.319), and may change in any Claude Code release; it is used on Robert's
  machine by his permission for his own sessions. A session whose `crossSessionInbound` is
  `hold` (user, repo or managed settings) will not act on it, and a bypass-permissions session
  may hold it because the sender attests no permission mode. Native Windows (named pipe) is
  UNVERIFIED. With it off, the pill shows no Play button and a push-to-talk hold between turns
  does nothing, as before.

- Barge hears nothing for the first 0.8-0.9 s of each reply (mic start-up; reported on macOS, n=6).
- On macOS, all 9 voice-processing channels measured identical; channel 0 is echo-free either way.
- External USB/HDMI/AirPlay speakers on a Mac count as headphones (plain capture) and may cut themselves off.
- voicemode-switch and upstream's update_config write voicemode.env without the native switch's lock.
- On a machine with both installs, voice-mode's walk-up from FELIX's runtime folder reaches the user's
  profile, so a personal `~/.voicemode/voicemode.env` would be read by FELIX's voice (FELIX-voice's finding).
  Our native launcher starts voice-mode in its runtime folder for this reason. Correction (reported by
  FELIX-voice, 2026-11-10, not re-run here): FELIX already stops that walk-up with
  `<runtime>\home\.voicemode.env` and its working folder, so this gap does not apply to FELIX today.
