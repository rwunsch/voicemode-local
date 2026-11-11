# Plan: the MCP server starts the voice pill; a user close sticks

Status: built, 2026-11-11. Branch `feature/pill-autostart`. Target tag v1.1.1.
Validated live on WSL (2026-11-11, n=1 per step): start on a turn, sticky ✕, quit with code 3,
and reopening on a fresh start. Native Windows ensure() validated 2026-11-11 (plain start, a job
with breakaway, a strict job with the fallback; n=1 each). macOS and a FELIX run are still to do.

## Decisions

- **(Robert, 2026-11-10)** Pill start-up moves into the MCP server, for every host, not only FELIX.
  pill-windows.ps1 ships in the voice_mode package. The server works out the start command per OS.
- **(Robert, 2026-11-10, via FELIX-voice)** Lifetime: the pill opens with the first voice session and
  closes with the last. If the user closes it, it stays closed until voice restarts from scratch.
  Applies to all users.
- **(FELIX-voice, agreed)** "Voice restarted from scratch" = no live registered voice-mode
  sessions. A session registers on its first converse turn, not at MCP start.

## Contract (agreed with FELIX-voice, 2026-11-10)

| Setting | Meaning | Default |
|---|---|---|
| `VOICEMODE_PILL` | `true` turns the pill on | off |
| `VOICEMODE_PILL_AUTOSTART` | `off`: the MCP server never starts or stops a pill window | on |
| `VOICEMODE_PILL_DATA_DIR` | the window's folder (Windows `-DataDir`: log, position, WebView2, mutex/stop names) | `<VOICEMODE_NATIVE_ROOT>\pill`, else `<base>/pill/window` |
| `VOICEMODE_PILL_PYTHON` | the backend's interpreter (natively) / the pill venv's python (macOS) | `sys.executable`; macOS `~/.voicemode/pill-venv/bin/python` |
| `VOICEMODE_BASE_DIR` | sessions, the closed marker, pause, overrides: `<base>/pill` | unchanged |

FELIX sets PILL=true, PILL_DATA_DIR=`<FELIX root>\pill`, CONTROL_CHANNEL_ENABLED=true, and on a Mac
PILL_PYTHON. It leaves AUTOSTART unset.

## Design

1. **Closed marker**: `<base>/pill/closed`. Written by the pill's ✕ (the `dismiss` command). While
   it exists the snapshot is `visible: false`. Replaces the in-memory `ctx.dismissed`.
2. **Clearing**: when a session enters its first turn and no other live session exists, the marker
   is removed: voice restarted from scratch. The backend also removes it when it quits.
3. **Window lifetime**: the backend quits once there have been no live sessions (and no recent
   push-to-talk) for a grace period (`QUIT_GRACE_S`). It never quits in its first `START_GRACE_S`.
   It prints `{"quit": true}` and exits with `EXIT_DONE` (3). Both wrappers close on that, and
   don't restart it.
4. **Autostart** (`voice_mode/pill_autostart.py`, stdlib only). It's called from `enter()` after
   registration, on every turn, and returns quickly. It acts only if the pill is on, AUTOSTART isn't
   off, there's no marker, and the backend lock isn't held. It then starts the window detached:
   - native Windows: `powershell.exe -STA … pill-windows.ps1 -BackendExe <python> -BackendArgs
     "-m voice_mode.voice_pill --stdio" -DataDir <data dir>`, with CREATE_NO_WINDOW |
     CREATE_BREAKAWAY_FROM_JOB, and without breakaway if the job refuses it.
   - WSL: `powershell.exe` with the package's pill-windows.ps1 through `wslpath -w`, and
     `-BackendCommand` = this interpreter + the package's voice_pill.py.
   - macOS: `<pill python> <package>/voice_pill_mac.py`, in a new session.
   A launch lock in `<base>/pill` stops two sessions from launching at the same moment.
5. **Launchers**: voicemode-mcp (macOS, WSL) and voicemode_native.py stop starting the window
   themselves. `voicemode-switch pill on` / `voicemode_native.py pill on` still start it at once.

## Open / to measure

- Whether the Copilot app's Job Object allows breakaway (FELIX-voice is measuring). If it doesn't,
  the window is started without breakaway, dies with the chat that started it, and comes back on
  the next turn of any other chat.
- macOS needs a FELIX-MAC run before the tag.
