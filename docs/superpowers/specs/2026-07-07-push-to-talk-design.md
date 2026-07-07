# Push-to-Talk Mode — Design

**Date:** 2026-07-07
**Status:** Approved by user (voice brainstorming session)
**Scope:** voicemode-local patch overlay on voice-mode; cross-platform (WSL2, native Linux, macOS, Windows native)

## Problem

Today, `converse` always listens using continuous silence-detection: it starts
recording, waits for the user to speak, and auto-stops after detecting
silence. That's fine for open conversation but has no way to:

- Start listening on demand rather than immediately after every TTS reply
- Interrupt a slow/wrong silence-detection trigger
- Barge in over Claude's TTS playback
- Do a deliberate hold-to-talk take, the way a two-way radio or Discord
  push-to-talk works

The user wants an opt-in **push-to-talk (PTT) mode**, toggled live through
conversation (not a shell command), using a hotkey that only acts while their
Claude Code terminal window has focus — not a true system-wide hook that
fires no matter what app is active.

Related upstream reference: voice-mode PR #328 (hold-to-record via
`pynput`/`evdev`, Linux only, still open) — this design goes further with
short-press/long-press semantics and cross-platform focus scoping.

## Goal

A `VOICEMODE_PTT_ENABLED` mode, off by default, where:

- **Short press** (< threshold) of a hotkey starts a normal silence-detection
  listen, same as today's default behavior. Pressing it again mid-listen
  manually ends the recording early — a manual override for slow or
  false-negative silence detection.
- **Long press / hold** (≥ threshold) starts recording immediately and stops
  the instant the key is released — true hold-to-talk, bypassing silence
  detection entirely.
- Pressing the hotkey while Claude's TTS is playing **interrupts (barge-in)**
  the playback and starts listening per the rules above.
- The hotkey only acts when the user's Claude Code terminal window has
  focus — no true system-wide hook across arbitrary unfocused windows.
- Toggled via the existing `update_config`/`config_reload` MCP tools
  (upstream `configuration_management.py`), not a new CLI command — the user
  asks in conversation ("turn on push to talk") and Claude calls the tool.

## Requirements (user-confirmed)

| Decision | Choice |
|---|---|
| Hotkey | Control-Space |
| Short vs. long press threshold | ~1 second (tunable later) |
| Short-press-again while listening | Manually stops the recording early, even if silence detection hasn't triggered |
| Barge-in over TTS | Yes — pressing the hotkey interrupts playback |
| Focus scope | Only while the specific Claude Code terminal window has focus; not a true system-wide hook |
| Platform scope | Cross-platform from the start: WSL2, native Linux, macOS, Windows native |
| Toggle surface | Existing `update_config` MCP tool → `voicemode.env`, not a new CLI switch |
| Default | Off — conversational (silence-detection) mode stays the default behavior |

## Architecture

### Why this needs a background listener, not just a converse-time check

`converse` only runs while the MCP server is actively inside a tool call. A
hotkey press can happen between calls (e.g. to start a fresh listen) or while
TTS is playing (barge-in) — both outside `converse`'s own execution window.
So key capture must be a **persistent background listener**, independent of
any single `converse` call, that communicates state to `converse` via a
small IPC channel.

```
~/.voicemode/ptt/
└── <session-id>.sock      # per-session Unix domain socket (POSIX)
                            # or named pipe \\.\pipe\voicemode-ptt-<session-id> (Windows)
```

The listener pushes JSON events (`{"type": "press"|"hold_start"|"release",
"ts": ...}`) to the socket; `converse`'s record/playback phases read from it.

### Per-platform key capture

| Platform | Listener location | Mechanism | Focus check |
|---|---|---|---|
| Native Linux (X11) | Same process/host as voice-mode | `pynput.keyboard` | `xdotool getactivewindow getwindowpid` (or `wmctrl`), compared against the terminal's PID resolved at session start |
| Native Linux (pure Wayland, e.g. stock GNOME) | n/a | **Known limitation** — Wayland compositors block global key hooks outside XWayland apps; `pynput` will not reliably see keys. Document as unsupported for v1; fallback is the terminal-keybinding approach (see Alternatives) |
| macOS | Same process/host | `pynput.keyboard` (needs one-time Accessibility permission grant — cannot be automated) | `NSWorkspace.sharedWorkspace().frontmostApplication()` via `pyobjc`, compared by bundle id/pid |
| Windows native | Same process/host | `pynput.keyboard` or `ctypes` + `SetWindowsHookEx` | `GetForegroundWindow()` + `GetWindowThreadProcessId()` via `ctypes`/`pywin32` |
| **WSL2** | **Windows side** — a small companion script, since the terminal (Windows Terminal) is a Windows process while voice-mode runs inside the Linux guest; WSLg is a nested compositor voice-mode cannot hook into from inside | Windows-side `pynput`/`ctypes` hook, forwards events over a local TCP port to the WSL-side listener | Windows-side `GetForegroundWindow()` check, same as native Windows |

This mirrors the existing precedent in this project of using Windows-side
Chrome for CDP work that Linux Chrome can't do (corp cert issue) — some
capabilities are only reachable from the Windows host when running under
WSL2.

### Identifying "this session's terminal"

With multiple concurrent Claude Code sessions (see the session-queue design),
PTT must not fire for the wrong session's terminal. **v1 heuristic:** at
first PTT-enabled `converse` call, the listener resolves the terminal window
by walking the process tree from the MCP server's PID up to the terminal
emulator, then (X11: `xdotool search --pid`; Windows: match `WT_SESSION`/
window title against a per-session marker written to the environment).
**Known limitation:** two sessions with identical terminal titles in the same
working directory can be ambiguous; documented as a v1 gap, not solved here.
If it proves unworkable in practice, fall back to requiring the user set a
distinguishable terminal tab title per session (already recommended practice
per the session-naming design).

### Converse integration

When `VOICEMODE_PTT_ENABLED=true`:

- **Listen phase:** instead of starting silence-detection immediately,
  `converse` blocks on the PTT socket for a `press`/`hold_start` event.
  - `press` (released before threshold): behaves exactly like today's
    silence-detection listen. A second `press` event during this listen is
    treated as a manual stop signal (early cutoff).
  - `hold_start` → recording starts immediately; a paired `release` event
    stops it. No silence detection runs in this path.
- **Playback phase:** a background watcher on the PTT socket during TTS
  playback; any `press`/`hold_start` event cancels playback immediately
  (barge-in) and falls through to the listen phase above.
- Patched the same way as the session queue and audio-token features: a new
  `patches/ptt.py` module (pure logic, unit-testable without audio) plus a
  surgical, pattern-anchored patch to `tools/converse.py`'s listen/playback
  phases via `patches/apply.sh`. If upstream `converse.py` has drifted and
  the patch anchors don't match, the patcher fails loudly rather than
  half-patching — same contract as the existing patches.
- When `VOICEMODE_PTT_ENABLED=false` (default), `converse` is byte-for-byte
  unaffected — the PTT code path is not entered and the listener process is
  never spawned.

### MCP toggle

No new tool. `VOICEMODE_PTT_ENABLED` (bool, default `false`) and
`VOICEMODE_PTT_HOTKEY` (default `ctrl+space`) are read/written through the
existing `update_config`/`config_reload` tools, same as any other
`voicemode.env` key. Turning it on for the first time spawns the listener
process (and, under WSL2, verifies the Windows companion is reachable,
surfacing a clear error with setup instructions if not).

### Auto-start

The listener process only starts when `VOICEMODE_PTT_ENABLED=true`, following
the same "detached background process, checked by a port/socket file to
avoid duplicates" pattern already used for `whisper-proxy`. It is not started
unconditionally on shell init like the whisper proxy, since spawning a global
key hook (and on macOS, prompting for Accessibility permission) when the
feature is off would be an unwelcome surprise.

## Alternatives considered

**Terminal-keybinding approach (rejected as primary, kept as fallback):**
instead of an OS-level global hook, bind the hotkey inside the terminal
emulator itself (Windows Terminal `keybindings.json`, iTerm2 key mapping,
etc.) to shell out to a small script that hits the PTT socket. This is
inherently focus-scoped (terminal keybindings only fire when the terminal
owns the keystroke) and sidesteps the WSL Windows-bridge complexity and the
Wayland global-hook restriction entirely. Rejected as the primary design
because most terminal keybinding systems fire once on key-down, not on
key-up — cleanly distinguishing short-press from long-hold would require
faking it with double-tap-vs-single-tap semantics instead of a true
press/hold/release, which the user explicitly wants. Documented here as the
recommended manual fallback for pure-Wayland Linux desktops where the
OS-hook approach doesn't work.

## Testing

- **Unit** (pure logic, no audio, no real key hardware): press/hold/release
  state machine and threshold classification; barge-in cancellation signal
  path; PTT-disabled path is a no-op passthrough.
- **Integration:** a fake event source feeding synthetic press/hold/release
  events into the socket, asserting `converse`'s listen/playback phases react
  correctly (short-press listen, hold-to-talk, manual early-stop, barge-in).
- **Manual acceptance, per platform:** WSL2 (primary), native Linux (X11),
  macOS, Windows native — hold-to-talk, short-press with manual stop,
  barge-in over an active TTS reply, and confirming a background window
  losing/gaining focus does not spuriously trigger recording.

## Out of scope

- True system-wide hotkey (fires regardless of focused window) — the
  existing roadmap item "global hotkey to resume voice mode" is a related
  but separate feature.
- Disambiguating multiple sessions with identical terminal titles (v1
  heuristic gap, noted above).
- Pure-Wayland native Linux support via OS-level hook (fallback documented,
  not implemented as primary path).
