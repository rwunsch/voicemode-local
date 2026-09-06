# Push-to-talk: investigated 2026-09-05 — verdict and plan

**Status:** investigation complete. Recommendation below. Nothing submitted.

## Verdict

**Rebuild our PTT as a control-channel client.** Upstream's 8.11 control channel already
provides everything except one thing, and that thing is ~100–150 lines rather than our 1,984.

## What upstream already gives us (validated in 8.12.0)

`voice_mode/control_channel.py` (503 lines) — pure logic, transport-agnostic, with
`control_socket.py` on top and a `voicemode control {pause,resume,stop,skip_back,skip_forward}`
CLI (`cli.py:1443-1510`).

The decisive finding: **the control channel already reaches the recording loop, not just
playback.** `converse.py:1483-1506` polls `get_control_state().snapshot()` inside the capture
loop and honours `is_skip_forward` there:

```python
if snap.is_skip_forward:
    logger.info("⏭  Recording ended early by skip_forward -- transcribing what we have")
    break
```

Mapping that against our own design doc (`../superpowers/specs/2026-07-07-push-to-talk-design.md`):

| Our PTT behaviour | Upstream 8.12.0 | Gap |
|---|---|---|
| Press while assistant speaks → cut TTS, hand over mic | `skip_forward` — documented as exactly this, lands in ~200ms | **none** |
| Short press while listening → stop recording early, transcribe | `skip_forward` in the recording loop (`converse.py:1506`) | **none** |
| Hold → record while key is down, ignore silence detection, end on release | — | **this is the whole gap** |

Also already solved upstream and worth not rebuilding: peer-credential auth on a `0700`
socket dir, bounded input, stale-socket cleanup (VM-1688 adversarial review), plus a
server-owned intent allowlist so a control client can never inject free text into the
agent's context.

## The one real gap

A **level-triggered hold**. `skip_forward` is edge-triggered; PTT needs "the mic is open
exactly while the key is down", and critically needs silence detection suppressed for the
duration — otherwise pausing mid-thought ends your turn, which defeats the point.

`disable_silence_detection` already exists (`converse.py:1335`) but only as a **call-time
argument** threaded down from the CLI/config. It cannot be toggled at runtime.

**Proposed upstream shape** (small, additive, no behaviour change when unused):

1. `COMMAND_HOLD_START` / `COMMAND_HOLD_END` added to `VALID_COMMANDS`.
2. `request_hold_start()` / `request_hold_end()` + an `is_holding` field on `ControlState` —
   mirroring the existing `request_skip_forward` / `is_skip_forward` pair exactly.
3. In the recording loop: while `snap.is_holding`, skip the silence-detection exit; on
   hold-end, `break` — the same path `skip_forward` already takes.
4. `voicemode control hold-start` / `hold-end` CLI verbs for parity.

Everything else stays ours and out-of-tree: X11/Windows/WSL2 key capture, focus scoping, the
press/hold classification, the relay bus. That is the right split — upstream already delegates
key capture to Hammerspoon and Stream Deck rather than owning it.

**Honest caveat:** item 3 touches the recording loop, not only `ControlState`. And a press
while converse is *not* in its listen phase still cannot originate a turn — the agent decides
when to listen. So this delivers hold-to-talk *within* a listen window, which is what the
design doc's hold mode actually needs, but it is not "press anytime to summon the mic".

## On PR #328

`feat: hold-mode PTT with TTS interrupt, tag-based polyglot TTS, and local TTS retry` —
opened 2026-03-27, **`updatedAt` still 2026-03-27**, **0 comments**, +1442/−60 across 12 files.

Two reasons it has sat for five months, and both are instructive for us:

1. **It bundles three unrelated features** — PTT, tag-based polyglot TTS, and local TTS
   retry. A reviewer has to make three decisions to merge one thing. This is precisely the
   "one fix per PR" rule in our own [README](README.md), demonstrated in the negative.
2. **It has been overtaken.** It predates the control channel by four months, so its barge-in
   machinery — an eager global `pynput` listener, a `_current_playback` global,
   `interrupt_streaming` polled inside the PCM chunk loop, `stream.abort()` — solves a problem
   upstream independently solved in 8.11 with `skip_forward`. Its TTS-interrupt half is now
   redundant; only the hold semantics remain novel, and those are the ~100 lines above.

**Recommendation:** do **not** open a rival PR. Comment on #328 noting the control-channel
overlap and offering the narrower hold-intent path, and comment on #312 with the same. If the
author would rather split #328, say so supportively — a review from someone running a working
PTT is worth more to that PR than a competing diff. Only open our own PR if #328 stays dormant
after that.

## Local plan (independent of upstream — you get PTT either way)

1. Add the `hold` intent to our patched `control_channel.py` + recording loop as a small
   local patch (`patch_control_hold.py`), replacing `patch_converse_ptt.py` and
   `patch_core_ptt.py`. Both of those target code that went 2261 → 4620 lines and will not
   re-anchor cheaply.
2. Repoint our existing listeners (`ptt_listener_linux.py`, `ptt_listener_windows.py`,
   `ptt_bridge.py`) at upstream's control socket instead of our own TCP relay bus — deleting
   `ptt_ipc.py` and `ptt_playback_bridge.py`, since upstream now owns barge-in.
3. Keep `ptt_core.py` (press/hold classification) and the focus scoping — still ours.

**Estimated:** ~1,984 lines → roughly 400, most of it the platform key listeners, and only
one small patch against upstream code instead of two against the most volatile file in the
package.

---

## Live-test findings, 2026-09-06

Three live attempts through the real MCP server all failed before the mechanism was
proven working in isolation. Recording what each step established, because the sequence
matters more than the conclusion.

### 1. The patch was inert (fixed)

`ControlCommand.apply_to()` had no arm for `hold_start`/`hold_end`. `parse_command`
accepted them and nothing happened. All 15 unit tests passed because every one drove
`ControlState` directly and never touched the dispatch path the socket listener actually
uses. Fixed, plus three tests including one that walks every `VALID_COMMANDS` entry
through `apply_to`.

### 2. The chain does work (proven)

`socket_path_test.py` runs the real `start_control_listener()` and the real
`record_audio_with_silence_detection()`, and sends `hold_start` over the real AF_UNIX
socket from a **separate process**:

```
  sent hold_start: True
  audio=308.01s  wall=42.94s
  final is_holding: True
  VERDICT: SOCKET->HOLD WORKS
```

Instrumented per-iteration, the loop sees `is_holding=True` from t=0.51s onward. Baseline
without a hold ends at 1.53s on the silence exit. So client -> socket -> listener ->
apply_to -> ControlState -> recording loop is sound.

### 3. What still fails: a hold started during TTS playback

In every live attempt the probe's `hold_start` landed during playback rather than during
the listen phase (playback ran 11.0s, then 20.5s; the probe's fixed delays under-shot
both). Those recordings ended on the normal silence exit.

**Likely cause, not yet confirmed by measurement:** `ptt_control_client.on_action("hold_start")`
sends `skip_forward` first when audio is playing (to barge in), and converse consumes that
edge at `converse.py:3826` with `control_state.reset()` -- which our patch made clear
`_holding` too. So the barge-in wipes the hold it was supposed to precede.

This matters for real use: pressing the key *while the assistant is speaking* is the
normal way to interrupt and answer.

**Candidate fixes, in preference order:**

1. Have `reset()` preserve `_holding` while keeping it clearing the play/hold/cut state.
   Turn-boundary leakage is still prevented by the scope-entry reset, which happens before
   any key press for that turn.
2. Re-send `hold_start` after the skip_forward edge is consumed (client-side retry) --
   works, but races the consume and is fragile.
3. Give the hold its own consume path so the skip_forward edge never touches it.

(1) is cleanest and is a two-line change to `patch_control_hold.py`. Verify with the same
`socket_path_test.py` harness, extended to send `skip_forward` immediately before
`hold_start`.

### Method note

Two wrong conclusions were drawn and retracted along the way: a first in-process test
reported "HOLD DID NOT SUPPRESS" because the fake stream runs ~30x real time and the
wall-clock driver fired after the recording had already finished; and the multi-session
socket was suspected before `lsof` showed the right process (46597) owned it. Both were
harness faults, not code faults. Drive these tests in **audio time**, never wall time.
