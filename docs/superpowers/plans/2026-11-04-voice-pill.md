# Voice Pill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small always-on-top window on Windows and Mac that shows voice-mode state and gives its controls.

**Architecture:** One Python backend per machine (`voice_pill.py`) reads shared state files and the control socket, and speaks JSON lines over stdin/stdout. One HTML interface renders its snapshots inside a thin native window per OS. No network port: on Windows the window runs the backend as a `wsl.exe` child process.

**Tech Stack:** Python (numpy-free stdlib for state and backend), vanilla HTML/JS with `node:test`, PowerShell 5.1 + WPF + WebView2 on Windows, pywebview on Mac.

**Spec:** `docs/superpowers/specs/2026-11-04-voice-pill-design.md`

## Global Constraints

- voice-mode stays pinned at 8.12.0. Patches are idempotent, fail loudly on anchor drift, and are tested against pristine sources (`VM812_SRC`).
- No network listener. The control path is the owner-only state directory and the existing socket.
- The pill never opens the mic, and polling stays cheap: one snapshot a second when idle, four a second during a voice turn.
- Show real state only. No fake waveform. When the backend is unreachable, the UI says so instead of showing the last state.
- Same behaviour on Mac and Windows; a difference needs a stated reason.
- No heavy install: nothing a user must install beyond what the wrapper fetches with a pinned checksum.
- Keep it small. If a task's production code passes about 300 lines, stop and reconsider.
- Commit messages and docs carry no AI attribution (user's global rule).

## Review Focus

1. **Pause left on when the pill dies.** Every voice session would hang silently. The paused flag records the backend's pid, and a dead pid means not paused. Test in Task 1.
2. **A crashed session's file, including pid reuse.** It must not keep the pill up. Registration stores pid plus process start time. Test in Task 1.
3. **Backend dies or WSL shuts down while the window is open.** The UI shows "Disconnected", and the wrapper restarts the backend with backoff. Test in Task 5 (UI), manual check in Task 6.
4. **Second pill started by accident.** The second instance exits, leaving one window. Test in Task 4.
5. **Socket vanishes mid-command.** Clicking Interrupt between turns does nothing harmful and raises nothing. Test in Task 4.

---

### Task 1: Shared state module

**Files:**
- Create: `patches/voice_pill_state.py` (installed as `voice_mode/voice_pill_state.py`; stdlib only)
- Modify: `patches/apply.sh` (add to the copy list)
- Test: `tests/test_voice_pill_state.py`

**Interfaces:**
- Produces, all taking an optional `base: Path` defaulting to `~/.voicemode/pill` (created 0700):
  - `register_session(pid: int, label: str, voice: str | None) -> None`, `touch_session(pid: int, voice: str | None = None) -> None`, `unregister_session(pid: int) -> None`
  - `live_sessions() -> list[dict]`, each `{pid, label, voice, started_at, last_voice_at}`; removes files whose pid is dead or whose process start time differs
  - `request_stop(pid: int) -> None`, `take_stop(pid: int) -> bool` (consumed once)
  - `set_paused(on: bool, owner_pid: int) -> None`, `is_paused() -> bool`
  - `set_override(pid: int, voice: str | None, language: str | None) -> None`, `get_override(pid: int) -> dict`
  - `record_interruption(t: float) -> None`, `interruptions_since(t: float) -> int`
  - `touch_ptt() -> None`, `ptt_recent(window_s: float = 120.0) -> bool`
  - `load_ui() -> dict`, `save_ui(ui: dict) -> None`

- [ ] **Step 1: Write the failing tests** in `tests/test_voice_pill_state.py`: `test_dead_pid_session_is_removed`, `test_reused_pid_with_different_start_time_is_removed`, `test_take_stop_is_consumed_once`, `test_paused_with_dead_owner_is_not_paused`, `test_paused_with_live_owner_is_paused`, `test_interruptions_counted_since_time`, `test_ptt_recent_expires_after_120s`, `test_ui_state_round_trips`, `test_corrupt_files_read_as_empty`, `test_state_dir_is_0700`.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_voice_pill_state.py -q`. Expected: FAIL, module missing.
- [ ] **Step 3: Implement.** Atomic writes via temp file + `os.replace`. Process start time from `/proc/<pid>/stat` on Linux and `ps -o lstart=` on Mac.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(pill): shared state files`.

### Task 2: Server patch

**Files:**
- Create: `patches/patch_voice_pill.py`
- Modify: `patches/apply.sh` (after `patch_voice_barge`; add targets), `patches/voice_barge_tool.py`, `CLAUDE.md`
- Test: `tests/test_voice_pill_patch.py`

**Interfaces:**
- Consumes: Task 1 functions; `ControlState.set_speaking` from `patch_voice_barge`.
- Produces:
  - `ControlState.set_listening(bool)`, `.is_listening`; `"listening"` in the status reply
  - `speaking` reported by `stream_pcm_audio`, `stream_cartesia_pcm` and `stream_with_buffering` too, using the same `_inner` wrapper pattern (required so the Mac can show "Speaking", and so barge-in is no longer inert there)
  - MCP tool `voice_stop()` removing this session from the pill; its description says to call it when the user says "stop voice mode"

Behaviour in `converse` (each gets a test on the patched pristine source, using the module-level helper it calls):
- `_voice_pill_enter(voice) -> str | None` runs before the conch is taken. It registers or touches the session, applies an override, waits while `is_paused()`, and if `take_stop(pid)` returns True, unregisters and returns a message for the agent to return without speaking: `Voice was stopped from the pill. Continue in text unless the user asks for voice again.`
- A voice override replaces the caller's `voice`. A language override appends to the result: `The user selected <language>. Write your next messages in <language>.`
- If `is_paused()` turns true during recording, the loop ends, that audio is discarded, and the listen restarts after resume. In `speak` paths the existing control `pause` holds TTS.

- [ ] **Step 1: Write failing tests** in `tests/test_voice_pill_patch.py`: `test_status_reports_listening`, `test_listening_false_after_recording` (AST bracket check like `test_playback_is_bracketed_by_speaking`), `test_streaming_players_report_speaking`, `test_enter_returns_stop_message_once`, `test_enter_waits_while_paused_then_continues`, `test_override_replaces_voice`, `test_language_override_is_added_to_result`, `test_recording_during_pause_is_discarded`, `test_patch_is_idempotent`, `test_anchor_drift_fails_loudly`.
- [ ] **Step 2: Run** `VM812_SRC=<pristine> .venv/bin/python -m pytest tests/test_voice_pill_patch.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the patch and `voice_stop`. Put the helpers in `voice_pill_state.py` where they need no server imports, so they are unit-testable. Add one line to the CLAUDE.md voice section about `voice_stop`.
- [ ] **Step 4: Run the full suite.** Expected: all pass.
- [ ] **Step 5: Live check.** Run `apply.sh`, then one `converse` on a fresh process, polling status: `speaking` then `listening` then no socket; `live_sessions()` shows it throughout and after.
- [ ] **Step 6: Commit** `feat(pill): report listening, register sessions, pause and stop`.

### Task 3: Trigger integration

**Files:**
- Modify: `patches/voice_barge.py`
- Test: `tests/test_voice_barge.py`

**Interfaces:**
- Consumes: `record_interruption`, `is_paused`.
- Produces: `run(..., is_paused=lambda: False, on_fire=None)`.

- [ ] **Step 1: Write failing tests:** `test_mic_stays_closed_while_paused`, `test_open_mic_closes_when_pause_starts`, `test_fire_calls_on_fire_once`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement,** and wire `main()` to `is_paused` and `record_interruption`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(pill): trigger honours pause and records interruptions`.

### Task 4: Backend

**Files:**
- Create: `patches/voice_pill.py` (installed as `voice_mode/voice_pill.py`, run with `python -m voice_mode.voice_pill --stdio`)
- Modify: `patches/apply.sh`
- Test: `tests/test_voice_pill.py`

**Interfaces:**
- Consumes: Task 1, and `_status`/`control_socket_path` from `voice_barge.py`.
- Produces: `build_snapshot(status: dict, sessions: list, conch: dict, ...) -> dict` (pure), `handle_command(msg: dict, ...) -> None`, `main() -> int`.
- Snapshot, one JSON line:
  `{"visible": bool, "phase": "speaking"|"listening"|"working"|"paused"|"idle", "floor": {"pid","label"}|null, "sessions": [{"pid","label","state": "talking"|"waiting"|"working","position": int|null}], "voice": str|null, "language": str|null, "barge": {"enabled": bool, "available": bool, "count": int}, "platform": "windows"|"mac"|"linux", "ui": {...}}`
- Commands, one JSON line each: `{"cmd": "interrupt"|"pause"|"resume"|"stop"|"dismiss"}`, `{"cmd": "set_barge", "on": bool}`, `{"cmd": "set_voice", "voice": str}`, `{"cmd": "set_language", "language": str}`, `{"cmd": "save_ui", "ui": {...}}`.

Rules, pinned by tests:
- `visible` is true if any live session exists or `ptt_recent()`, and `dismiss` hides until a session newly registers.
- `phase`: `paused` if paused; else `speaking`/`listening` from status; else `working` if a session is live; else `idle`. The UI labels `working` as "Not in a voice turn".
- `floor` follows the conch holder, else the last holder.
- `stop` sends the control `stop` and calls `request_stop` for the floor session only.
- `barge.available` is false when `parec` is missing, which hides the switch on Mac.
- Conch data comes from the queue files directly, not by spawning the CLI once a second.

- [ ] **Step 1: Write failing tests:** `test_phase_precedence`, `test_visible_with_session_or_recent_ptt`, `test_dismiss_hides_until_new_session`, `test_stop_only_targets_floor_session`, `test_floor_falls_back_to_last_holder`, `test_interrupt_with_no_socket_is_harmless`, `test_pause_records_this_pid_as_owner`, `test_unknown_command_is_ignored`, `test_second_instance_exits` (flock on `~/.voicemode/pill/backend.lock`), `test_stdio_loop_emits_snapshots_and_exits_on_eof`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Exit on stdin EOF, so a closed window ends the backend and a Pause owner cannot outlive it.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(pill): backend snapshots and commands`.

### Task 5: Shared interface

**Files:**
- Create: `ui/pill/index.html`, `ui/pill/model.js`
- Test: `tests/pill_model.test.mjs` (run with `node --test`)

**Interfaces:**
- Consumes: the Task 4 snapshot and commands.
- Produces: a transport the wrappers implement: `window.pillTransport.send(jsonString)`, and the page's `window.pillReceive(jsonString)`. `model.js` exports `viewModel(snapshot, lastSeenMs, nowMs)`.

The layout follows the mockup in the spec: pill with state, Interrupt, Pause and Stop; expand for voice, language, Interrupt by voice, count and keys; dock and minimize controls. Minimized leaves a small dot. Label strings: Speaking, Listening, Not in a voice turn, Paused, Disconnected.

- [ ] **Step 1: Write failing tests:** `disconnected after 3s with no snapshot`, `pause button reads Resume when paused`, `interrupt disabled unless speaking or listening`, `barge switch hidden when unavailable`, `keys match platform` (Windows `Ctrl+Space`; Mac `Control-Option-Space` and `Control-Option-Escape`), `queue row shows position`.
- [ ] **Step 2: Run** `node --test tests/pill_model.test.mjs`. Expected: FAIL.
- [ ] **Step 3: Implement** with no framework, no external assets and a strict CSP. Support light and dark.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Show Robert.** Push the page with sample snapshots to the visual companion and adjust from his reaction.
- [ ] **Step 6: Commit** `feat(pill): shared interface`.

### Task 6: Windows window

**Files:**
- Create: `pill-windows.ps1`
- Modify: `voicemode-switch` (add `cmd_pill`: `on|off|status`), `install.ps1` if needed, `docs/voice-pill/README.md`
- Test: `tests/test_switch_ensure_kokoro_port.py` for `pill` config; manual checklist below

**Interfaces:**
- Consumes: Tasks 4 and 5. Runs `wsl.exe -d <distro> -- <repo>/.venv/bin/python -m voice_mode.voice_pill --stdio`.
- Produces: `VOICEMODE_PILL=true|false` in `voicemode.env`. Autostart through the Startup folder, as with push-to-talk.

The window is borderless, topmost WPF hosting WebView2 from the Microsoft.Web.WebView2 package, fetched on first run into `%LOCALAPPDATA%\voicemode-local` with a pinned SHA-256. Runtime 156 is confirmed on this machine. The page is the local file, using a `https://` virtual host mapping. A docked window uses `SHAppBarMessage` to reserve the edge. The UI hides or shows the window from `visible`, without closing it.

- [ ] **Step 1: Spike first.** Open the empty page in the window. Pass: always on top, draggable, and CPU under 1% idle. If WebView2 from PowerShell 5.1 fails, stop and bring it back to Robert before going on.
- [ ] **Step 2: Implement** the stdin/stdout bridge, with backend restart at 1 s, 2 s, 4 s and capped at 30 s.
- [ ] **Step 3: Manual checklist, recording results in the README:** pill appears on a real `converse`; states change correctly; Interrupt, Pause, Resume and Stop work; `wsl --terminate` shows Disconnected, then recovers; closing the window while paused releases Pause; position restored after restart; audio has no new stutter under a voice turn (`voicemode-perf`).
- [ ] **Step 4: Commit** `feat(pill): Windows window`.

### Task 7: Mac window

**Files:**
- Create: `patches/voice_pill_mac.py`
- Modify: `voicemode-mcp` (start one per machine, like `ensure_ptt_listener`), `docs/voice-pill/README.md`

**Interfaces:**
- Consumes: Tasks 4 and 5.

The window is pywebview with `frameless=True` and `on_top=True`, and the backend as a subprocess. `FELIX-MAC` confirmed on 2026-11-04 (n=1, throwaway venv, macOS 26.7.1, Apple silicon) that pywebview 6.2.1 with pyobjc 13.0 opens such a window, and that it sits on top with no title bar.

Not yet handled: while open, the menu bar showed "python" as the active app, so it probably takes focus and has a Dock presence. Set the accessory activation policy (`NSApplicationActivationPolicyAccessory`) so it does neither. Staying above full-screen apps and across Spaces is untested. Swift 6.4.2 is available if pywebview cannot do this; use a small `WKWebView` wrapper in that case.

The Mac checkout is `main` at `af0fcdf`, with local changes to `piper-proxy.py` and `voices/piper-voices.json` that must be left alone. Its venv is live, so pywebview goes into a separate venv for the pill.

- [ ] **Step 1: Push branch `feature/voice-pill`,** and ask `FELIX-MAC` to pull it and run `apply.sh`.
- [ ] **Step 2: Implement,** and have `FELIX-MAC` run the same checklist as Task 6, plus: opening the pill does not take focus from the frontmost app, and no Dock icon appears. Robert checks anything needing ears or macOS permissions. Screenshots stay on the Mac and are cropped to the pill. Barge-in stays hidden, per the spec.
- [ ] **Step 3: Confirm** Kokoro through `streaming.py` now shows "Speaking" (Task 2).
- [ ] **Step 4: Commit** `feat(pill): Mac window`.

### Task 8: Windows push-to-talk wake

**Files:**
- Modify: `patches/ptt_relay.py`, `ptt-listener-windows.ps1`
- Test: `tests/test_ptt_relay.py`

**Interfaces:**
- Consumes: `touch_ptt`, `live_sessions`.
- Produces: a press with no call running calls `touch_ptt()`, then wakes the last session with the same wake line as Mac: `[push-to-talk] listen now (converse, skip_tts=true)`.

**Known risk.** Mac selects the Terminal tab by tty and types into it. That does not carry over: TIOCSTI is disabled in this kernel (`dev.tty.legacy_tiocsti = 0`, `CONFIG_LEGACY_TIOCSTI` unset), and Windows Terminal has no way to select a tab by tty.

- [ ] **Step 1: Spike.** Have the session write a unique title to its tty (OSC 0), find that tab with UI Automation from PowerShell, select it, then send the line. Pass: the right tab receives it among several, in 5 of 5 tries with Claude Code rewriting titles. Check that the focused app receives no stray keys. If it fails, stop and tell Robert: this is the one place "same behaviour" may need his decision.
- [ ] **Step 2: Write failing tests:** `test_press_with_no_socket_requests_wake`, `test_press_during_call_does_not_wake`, `test_repeat_within_cooldown_wakes_once`, `test_no_live_session_does_nothing`.
- [ ] **Step 3: Run.** Expected: FAIL.
- [ ] **Step 4: Implement.** Wait for key release before sending, as Mac does.
- [ ] **Step 5: Run tests.** Expected: PASS.
- [ ] **Step 6: Live check** with two sessions open: the last voice session wakes and records, and the pill appears.
- [ ] **Step 7: Commit** `feat(ptt): wake the last voice session on Windows`.

### Task 9: Finish

**Files:**
- Modify: `docs/voice-pill/README.md`, `README.md`, `CLAUDE.md`

- [ ] **Step 1: Run the full suite** with `VM812_SRC` and `node --test`. Expected: all pass, 0 skipped.
- [ ] **Step 2: Write up** what was measured on each OS, with n, and what was not tested.
- [ ] **Step 3: Whole-branch review,** then merge after Robert approves.
- [ ] **Step 4: Commit** `docs(pill): usage and measurements`.
