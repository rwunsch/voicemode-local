# macOS Push-to-Talk, Stop and Wake

> **TL;DR:** On macOS one background listener (`voice_mode.ptt_listener_macos`) gives every voice
> session three keys. **Control-Option-Space** skips when tapped, and holding it keeps the mic
> open. **Control-Option-Escape** stops voice cleanly. When no call is running,
> **Control-Option-Space** wakes the session you last spoke with and goes straight to recording.
> `voicemode-mcp` starts the listener once for all sessions.

| Field        | Value                                  |
|--------------|----------------------------------------|
| Status       | Complete                               |
| Created      | 2026-10-20                             |
| Last Updated | 2026-10-20                             |
| Project      | voicemode-local (macOS, Terminal.app)  |

## Keys

| Key | During a voice call | No call running (idle prompt) |
|-----|---------------------|-------------------------------|
| Control-Option-Space, **tap** | `skip_forward`: cuts the reply / ends your turn | **Wake**: last session starts recording you |
| Control-Option-Space, **hold** (>1s) | `hold_start` … `hold_end`: cuts the reply, mic open until release, no silence cut-off, plus a 0.35s tail | (wake, as tap) |
| Control-Option-Escape | `stop`: reply cut, no listening, `converse` returns `[control: stop]`, agent continues in text | nothing |

Other keys in this setup: **Option-Space** is Handy (system-wide dictation into any text field).
**Control-Space** is left alone because macOS uses it for "select previous input source".

## Setup

1. `~/.voicemode/voicemode.env`: `VOICEMODE_CONTROL_CHANNEL_ENABLED=true` (outside the
   `voicemode-switch` managed block).
2. `pynput` in the venv (in the README install line). `patches/apply.sh` installs the listener
   and the streaming barge-in patch.
3. System Settings → Privacy & Security: enable your terminal app (Terminal) under
   **Accessibility** and **Input Monitoring**. Quit and reopen the terminal after granting Input
   Monitoring. Without it the event tap gets nothing and PTT is silently inert.
4. Restart Claude Code / reconnect the voicemode MCP.

Environment knobs: `VOICEMODE_PTT_MACOS_MODIFIERS` (default `ctrl,alt`),
`VOICEMODE_PTT_HOLD_THRESHOLD` (1.0s), `VOICEMODE_PTT_RELEASE_TAIL` (0.35s),
`VOICEMODE_PTT_WAKE=false`, `VOICEMODE_PTT_ENABLED=false` (no listener at all).

Files: log `~/.voicemode/logs/ptt-listener.log`, pid `~/.voicemode/run/ptt-listener.pid`.

## How it works

- **One listener per machine.** The control socket path (`~/.voicemode/control.sock`) is shared,
  so one listener serves every session. `voicemode-mcp` starts it detached. `mkdir` on a lock dir
  makes the start atomic, so two sessions launching at once spawn one listener, and a live pid in
  the pidfile means later sessions do nothing.
- **Keys** come from a pynput (Quartz) event tap. Both hotkeys are swallowed (`darwin_intercept`)
  so they never reach the focused app. Press/hold/release classification is the shared
  `ptt_core.PTTKeyState`, the same as the Linux and Windows listeners.
- **Wake** happens when the control socket is not accepting connections (no call running):
  1. Find the last conch holder's pid in `~/.voicemode/logs/events/*.jsonl`, then its tty
     (`ps -o tty=`). Detached or stale servers show `??` and are skipped.
  2. Wait until Control/Option are physically released.
  3. AppleScript selects the Terminal.app tab whose `tty` matches exactly (no window-title
     guessing), `do script` types `[push-to-talk] listen now (converse, skip_tts=true)`, then
     System Events presses Return.
  4. Claude Code receives that as a user message; the agent calls `converse` with `skip_tts=true`
     and records immediately.

## Pitfalls found while building it (2026-10-20)

| Symptom | Cause | Fix |
|---------|-------|-----|
| Listener saw no keys | Only Accessibility granted | Also grant **Input Monitoring** and restart the terminal |
| Hold during a reply did nothing; only a tap cut it | Kokoro on macOS plays through `streaming.py`, which `patch_hold_barges_in` did not cover | Patch extended to both streaming loops |
| Control-Option-Escape "stopped" voice by killing the tool call | Escape reached Claude Code, which interrupts the running tool | Swallow both hotkeys; send the control-channel `stop` |
| Wake line typed but never submitted | A 127-char line is taken as a **paste** by Claude Code, and the Return after it does not send | Keep `WAKE_TEXT` short (~50 chars) |
| Same, while the key was still held | Return pressed under held Control-Option arrives as Control-Option-Return | Wait for modifier release before typing and pressing Return |
| Wake line typed 3–5 times | Key auto-repeat re-fired the wake while held | Wake only on a fresh press |
| `do script` text alone never submits | Its newline lands in Claude Code's input box rather than submitting | Real Return via System Events |
| `TIOCSTI` injection into the tty | Blocked by macOS (`EPERM`) | Not usable |

## Limits

- Wake only targets **Terminal.app** tabs. Other terminals (iTerm2, Ghostty, VS Code) would need
  their own tab lookup.
- Wake briefly brings the target tab to the front.
- Keys act on whichever session holds the control socket. With several sessions talking at once,
  that is the conch holder.
- Linux and Windows: no auto-start or wake yet. The Linux listener gates on terminal focus by
  walking up its own process tree, so it must run inside the session (see
  `docs/superpowers/specs/2026-09-06-ptt-wake-last-session-design.md`).
