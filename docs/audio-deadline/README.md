# Audio deadlines and the loop watchdog (FELIX #2095)

| | |
|---|---|
| Status | built, unit + pristine-8.12.0 tests green; NOT yet seen on a real hang |
| Last updated | 2026-11-27 (v1.2.26) |
| Files | `patches/audio_deadline.py` (logic), `patches/patch_audio_deadline.py` (wiring), `patches/voice_pill.py` + `ui/pill/model.js` ("Not responding") |

## The field report
Mac, Copilot desktop, FELIX 0.9.1 (= v1.2.24), 2026-11-27: `converse` started 13:04:16Z and the
client gave up after 180 s; `voice_stop` (one unlink) at 13:08:20Z also timed out after 180 s;
the pill showed Speaking with no audio. So the voice server's event loop answered nothing.
**Which call hung is unknown** (no stack was taken).

## What upstream 8.12.0 does on the event loop (read in the code)
- `core.py` chimes and system audio: `player.play(..., blocking=True)` waits for the end of
  playback with **no timeout** (`audio_player.py` `wait()`). A device that stops calling back
  never ends it.
- `tools/converse.py`: `sd._terminate(); sd._initialize()` at the start of every converse.
- The control socket answers on its own threads, so its status kept saying `speaking`
  while the loop was dead. That is why the pill showed a stale Speaking.

## What we do
- **Deadlines.** Those calls run on a thread of their own. Deadline = the sound's length +
  `VOICEMODE_AUDIO_DEADLINE_S` (default 10; 0 = off, as before). A miss marks the process
  *wedged*: a stuck CoreAudio call cannot be cancelled from Python. That converse (and any
  later one) returns "Voice stopped: the audio device did not respond (...). The voice
  server exits now ... restart the chat (in FELIX, restart FELIX) ...", and the process exits
  with code 75 two seconds later.
- **A miss ends the turn at once** (code review of 33d87cb). upstream catches a chime's
  failure (`except Exception`) and carries on, and its TTS playback then waits on
  `asyncio.to_thread` with no deadline, so a miss alone did not end the turn. Now: once
  wedged, every later device call fails at once; the outermost wrapper runs the turn under
  `audio_deadline.guard`, which cancels it within 0.25 s of the miss and returns the message;
  and the exit is scheduled at the miss itself (after 30 s, `WEDGED_EXIT_CAP_S`), brought
  forward to 2 s once the reply has gone out.
- **Watchdog.** A loop task stamps a heartbeat every second; a thread checks it. Stuck longer
  than `VOICEMODE_LOOP_STALL_EXIT_S` (default 90, below the 180 s client timeout; 0 = off):
  every thread's stack goes to a stall file and the log, then exit 75 (without
  `logging.shutdown()`, which could block on a handler lock held by a thread stuck on stderr). `faulthandler.dump_traceback_later`
  (limit + 15 s, re-armed every 10 s, not every beat) is the backstop if the GIL itself is held. A checking thread that was itself
  away > 5 s means the machine slept, so the stamp is renewed instead.
- **Pill.** The status reply carries `loop_lag_s`. Above 10 s the pill shows **Not responding**
  ("Voice server not responding") instead of Speaking or Listening. There is no heartbeat file.

## Stall files (for diagnostics)
voice-mode logs only to stderr unless `VOICEMODE_DEBUG` is set (`config.py` basicConfig), and
a host may not keep stderr, so every dump also goes to a file of its own:

- `<VOICEMODE_BASE_DIR, else ~/.voicemode>/logs/stall/stall-<UTC yyyymmddThhmmssZ>.txt`
  (`-1`, `-2` ... when that second is taken). Reason, UTC time, pid, `loop_lag_s`, Python
  version and platform, then each thread's stack. Stacks only: no locals, tool arguments or
  message text (but they do contain file paths, e.g. the home folder). The last 5 are kept.
- `logs/stall/backstop-<pid>.txt`, opened when the watchdog starts, for faulthandler's
  backstop (it can only write to a file already open). Empty unless it fired; empty ones of
  voice servers that are no longer running are removed at the next start (never another live
  server's open file; on Windows only files no process holds open can be deleted anyway),
  non-empty ones kept (last 5).

## Log lines (logger `voicemode.audio_deadline`, level ERROR, stderr)
- `audio device not responding: <reason>; the voice server will exit after this turn`
- `the voice server's event loop was stuck for <N> s (limit VOICEMODE_LOOP_STALL_EXIT_S=<L>); exiting so a fresh one can start`
- `voice server stack dump (<why>) written to <path>`, then the same text as the stall file
- faulthandler also writes `Current thread ... (most recent call first):` blocks to stderr.

## Not known yet
- Whether the host restarts an exited server. Claude Code shows the MCP as failed (reconnect
  with `/mcp`); Copilot desktop and FELIX's `scripts/launch.js` do not respawn (FELIX-voice,
  2026-11-27). Hence the message says "restart the chat".
- Whether a legitimately slow loop (a very slow Windows laptop) ever exceeds 90 s. Not
  measured; `VOICEMODE_LOOP_STALL_EXIT_S=0` turns the exit off.
- After an exit the conch is released by the kernel flock and the dead holder is skipped
  (read in `conch.py`, not run live).

## Behaviour changes
- behaviour change: a stuck audio call or event loop ends the voice server (exit 75), old behaviour via `VOICEMODE_AUDIO_DEADLINE_S=0` and `VOICEMODE_LOOP_STALL_EXIT_S=0`
