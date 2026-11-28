# Moving voicemode-local from voice-mode 8.12.0 to 9.2.0: assessment (2026-11-28)

Why: Robert accepted (2026-11-28, via FELIX-voice) to assess the move to upstream 9.2.0 **before**
building Silero end-of-turn detection, so it is not built twice (FELIX #2107). Upstream 9.1.0
(2026-11-22) ships six of our merged PRs (#538, #552, #553, #555, #556, #587).

## Method
- Every step of `patches/manifest.json` (45 patch-target steps) and `manifest.windows.json` (48)
  applied to a fresh unpacked 9.2.0 wheel, continuing past failures.
- Every patched upstream function diffed 8.12.0 against 9.2.0, and each edit's distance to an
  upstream change measured.

Result: POSIX applies 36 of 45 and fails 9; native Windows applies 37 of 48 and fails 11.

Tiers: **validated** means I re-checked it myself in the 9.2.0 source. Everything else comes from a
research agent's per-step classification, which reads code but is not re-checked by me.

## The two dangerous ones: they apply cleanly and are still broken
- **`patch_control_transport` (Windows), validated.** 9.2.0's `_require_unix_sockets()` (#588) runs
  before our named-pipe branch (patched `control_socket.py:356` vs `:358`). On native Windows the
  control channel would never start: no PTT, no pill buttons, no barge-in, and no apply error to
  warn about it.
- **`voice_fallback` → simple_failover, validated.** 9.2.0 resolves a voice before sending anything
  (VM-1901). For a voice it cannot place it returns `voice_resolution_failed` with
  `attempted_endpoints: []` (simple_failover.py:251-267). `unknown_everywhere()` needs failed local
  attempts (voice_fallback.py:63), so the fallback to an installed voice never happens.

## Per-step classes (agent; POSIX manifest)

| Class | Count | Steps |
|---|---|---|
| Obsolete | 6 | turns_playback_stop (#555); conch_wakers ×4 (callback mode removed in 9.0.0, VM-2078); load_scipy_early (VM-2254) |
| Knock-on failure | 0 | (Windows: native_client_folder, which needs session_name's line) |
| Port, small | 5 | simple_failover (keep only the TTS-timeout edit); session_name (fold our name sources into `_conch_agent_name`); voice_language (tool description cut, VM-2442); control_transport; voice_fallback |
| Port, medium | 2 | conch_fairness (`release(spoke=)`, VM-2045); audio_deadline → converse (the device refresh now sits in a `try/except Exception` that would swallow `DeadlineMissed`) |
| Check | 5 | echo_labels (no-op: VM-2442 removed the echo instruction); light checks on tts_chunks, stt_language, voice_pill → converse, ptt_instant → converse |
| Applies, unchanged area | 27 | |

Windows only: windows_tempfile → core and → streaming are obsolete (#587).

## Interactions and behaviour changes for users (agent)
- **`conch_mode` / `VOICEMODE_CONCH_MODE` are removed in 9.0.0.** CLAUDE.md and our docs still
  describe them, along with `conch_wakers`.
- **Unknown voices fail before any request.** Inferred: Piper `p_de_*` or `dm_martin` may be
  "unresolvable" until provider discovery has run. Needs a live test.
- **New heard log.** Every reply writes `heard_*.jsonl`, with no setting. `heard.py` imports
  `fcntl`. Inferred: a warning per turn on native Windows unless a shim covers it.
- **Changed defaults.**
  - `VOICEMODE_CARTESIA_MODEL` sonic-3 → sonic-3.6.
  - New `VOICEMODE_TTS_VOICE_SUBSTITUTION` (default false; matches our policy).
  - The agent's scripted diff found no other `VOICEMODE_*` default changed.
- **New packages.** `listen/` and `mouth/` (with Silero in `listen/silero.py`, MIT) are new; converse
  imports neither.

## Effort
About 7 ports: 5 small, 2 medium. Six patches retire, five need checks.

The agent recommends doing control_transport and voice_fallback first, because nothing flags them.

The agent's working files (scripts, diffs, step map) are in the session scratchpad only.
