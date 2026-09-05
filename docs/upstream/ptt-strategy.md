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
