# Voice pill: a small on-screen control for voice mode

Status: draft for Robert's approval, 2026-11-04. Decisions marked **(Robert)** are his.
Those marked **(my call)** are mine and open to challenge.

## Why

Voice mode has no visible state. Robert runs many sessions at once and cannot see whether
the assistant is talking or listening, which session has the floor, or which keys do what.
Every control lives in a terminal or a hotkey he has to remember. The trigger was a mockup
from another voice app showing a status orb, a voice selector, and Pause and End buttons.

Success: during a voice conversation Robert can glance at one small thing, know what is
happening, and act on it without finding a terminal.

## What it shows and does

**(Robert)** The content he asked for:

- Whether the assistant is talking or listening
- Pause and Interrupt
- The push-to-talk keys, for Windows and Mac
- Interruptions: he wants to see them happen
- Stop voice mode
- Selecting voices and languages

**(my call)** Also shown:

- Which session has the floor and who is queued, from `voicemode conch status`. Other
  voice apps do not show this, and Robert runs many sessions.
- The Interrupt-by-voice on/off switch. It already exists as `voicemode-switch barge`.

## Form

**(Robert)** It starts as a small pill. Clicking expands it. It can dock to the right edge of
the monitor, or somewhere else. It must be small, and he must be able to minimize it away
when he does not need it. The thing that would make him turn it off is it being in the way
all the time.

Mockup: `.superpowers/brainstorm/871-1793830428/content/pill-or-panel.html`. Layout and
colours are a starting point, not a spec.

**(Robert, 2026-11-05)** Docked is not a full-height strip. It floats on the right edge, always
on top, in the middle by default, and moves only up and down that edge. An arrow slides it out of
view, leaving only an arrow that brings it back. The side edges of the window change its width.
This replaces my earlier call that docking reserves screen space.
**(Robert, 2026-11-05)** The full panel takes too much room as the docked form. **(my call)** Docked
starts as a slim rail with status and the three main controls, and the full panel opens from it.

**(my call)** Only one pill per machine, covering all sessions.

## When it is visible

**(Robert)**

- Not shown until voice mode starts in a session.
- Appears when a voice conversation starts, or when push-to-talk is pressed.
- Stays up the whole time the agent is working between turns. Idle time does not hide it.
- **(Robert, 2026-11-05, replaces the "stop voice mode" part of the next line)** Stopping one session
  does not remove the pill, because other sessions may use it and a stopped session can be
  re-engaged. It closes when every voice-mode server has exited, or when the X is pressed.
- Removed when the user says "stop voice mode", or two minutes after the last push-to-talk
  use, if push-to-talk is what started it.
- On return it has the same state as when it left: position, docked or not, expanded or not.

**(my call)** It is also removed when the session that started voice mode has ended, so it
cannot stay on screen forever. With several voice sessions, it stays until the last one ends.

## Platforms

**(Robert)** Runs on both Windows and Mac from the first version, and is tested on both.

**(Robert)** Mac testing runs through the `FELIX-MAC` session, because this machine cannot
reach the Mac directly. Work goes on a branch that session can pull.

**(my call)** One shared interface in a small always-on-top window, with a thin wrapper for
each OS. The primary Windows setup runs voice-mode inside WSL2 with the window on the
Windows side, so the two must talk across that boundary.

Native Linux is not a goal, but nothing should rule it out.

## Decisions from the design review

**(Robert)**, 2026-11-04:

- **Same behaviour on Mac and Windows** is the rule. Differences need a reason.
- **Interrupt by voice on Mac** is hidden in the first pill version and added right after. It
  needs new mic capture, permissions and echo testing. The trigger depends on `parec`, and
  `speaking` does not cover the streaming path, which the first version must add so the Mac
  pill can show "Speaking".
- **Pause covers the whole machine.** No session can speak or open the mic until resume.
- **The Stop button stops only the session currently talking.** Others are left alone.
- **Windows gets push-to-talk wake, like the Mac:** a press with no call running wakes the last
  voice session and starts recording. The pill appears at that point. This is in scope, and it
  resolves the conflict with "not shown until voice mode starts".

**(my call)**, open to challenge:

- If an agent stops calling `converse` without anyone saying "stop voice mode", no timeout
  hides the pill. It shows a neutral label such as "Not in a voice turn", and a dismiss
  control removes it. A wrong "Listening" is the worse failure.
- With no floor holder, the status and picker follow the last session that held the floor.
- **(Robert, 2026-11-05)** Minimizing goes to the Windows tray. **(my call)** The Mac equivalent is
  the menu bar, and the icon's colour follows the phase. It stays minimized when new voice activity
  starts, and the state survives its return.
- A `converse` call during machine-wide Pause waits, and resumes on resume. Audio already
  recorded when Pause is pressed is discarded, because of "Whisper must not hear anything".
- After a push-to-talk wake leads to a real conversation, the conversation rules apply and
  the two-minute rule no longer does.
- "Tested on Mac" means a checklist run through `FELIX-MAC` plus Robert for anything needing
  eyes, ears or macOS permissions.

## What exists today (read in the code, 2026-11-04)

| Need | State |
|---|---|
| Speaking or not | `speaking` in control-channel status (`patches/patch_voice_barge.py`) |
| Interrupt, Stop | `skip_forward`, `stop` control commands |
| Interrupt-by-voice switch | `voicemode-switch barge`, `voice_barge` MCP tool |
| Session queue | `voicemode conch status --json` |
| Push-to-talk events | Windows listener and relay, Mac listener |
| Interruptions | Logged by the trigger to `/tmp/voice-barge.log`, one line each |
| Speaking/listening/idle phase | Upstream writes `~/.voicemode/state.json` from the `turns[]` loop, for consumers like this. Check it before building gap 1; plain `converse` may not be covered |

Gaps that have to be built:

1. **Listening vs thinking.** Status reports only speaking or not. The control socket exists
   only during `converse`, so "no socket" means the agent is working.
2. **Whether voice mode is active.** Nothing records this. A session between `converse`
   calls looks identical to one that has stopped.
3. **"Stop voice mode".** The agent stops by simply not calling `converse` again, so nothing
   is signalled. The `stop` command only ends the current turn.
4. **Pause.** Today's `pause` holds TTS only. **(my call)** Pause means both speech held and
   mic closed, since that is what a Pause button promises. Whisper must not hear anything.
5. **Voice and language picker.** The agent passes `voice` on every call, so a picker has to
   override that. Two rules constrain it:
   - **(existing rule)** A voice speaks only its own language: German voices need German text.
     So changing language must also tell the agent to change the language it writes in.
   - **(existing rule)** Sessions use distinct voices so Robert can tell them apart. **(my
     call)** The picker therefore changes the session that has the floor, not all sessions.

## Non-goals

- Replacing the terminal or showing the transcript.
- A level meter or waveform driven by fake data. Show real state only; animation may
  reflect state, but must not imply audio it isn't measuring.
- Upgrading to voice-mode 9.x. We stay on the 8.12.0 pin, with patches, for this work.
- A different beep sound. Robert raised it and we parked it. Beeps are off for now.

## Anti-goals

Failure even if it technically works:

- It is in the way, or he cannot get rid of it quickly.
- It shows a state that is wrong. A pill claiming "Listening" with a closed mic is worse
  than no pill.
- It slows or stutters the audio. This machine has a history of CPU-starvation stutter, so
  polling must be cheap.
- It opens the mic itself or keeps it open. The mic stays closed outside voice turns.
- It needs a heavy install on a work laptop.

## Constraints

- Keep it small, in the spirit of the push-to-talk rewrite (~400 lines replacing 1,984).
- New voice-mode changes follow the repo's patch pattern: idempotent, loud on anchor drift,
  verified against 8.12.0, with tests against pristine sources (`VM812_SRC`).
- Nothing listens on a network port reachable from outside the machine, and any local
  control path needs the same care as the control socket (owner only).
- Follow `docs/` conventions, and document what was measured, with n.

## Left to the builder

- The window technology on each OS, and how it is packaged, within "no heavy install".
- How Windows reaches WSL. A `wsl.exe` child process speaking over stdin/stdout would avoid
  a port, but this is a suggestion.
- How active/stopped is recorded, and how the agent signals "stop voice mode".
- Polling rates, within the CPU constraint.
- How the pill is started (auto-start from `ensure` or the login startup folder, as with
  push-to-talk).
- Visual polish.
