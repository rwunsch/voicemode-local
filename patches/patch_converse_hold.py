#!/usr/bin/env python3
"""Consume the PTT hold flag in voice_mode/tools/converse.py's recording loop.

Pairs with patch_control_hold.py, which adds ``ControlSnapshot.is_holding``.
That patch alone is inert; this one gives it meaning.

Two edits, both inside record_audio_with_silence_detection:

1. **Suppress the silence exit while holding.** Upstream ends the turn once
   ``silence_duration_ms >= SILENCE_THRESHOLD_MS``. That is exactly what must
   NOT happen during a push-to-talk hold: the whole point is that you can pause
   mid-thought with the key down and keep the floor. While ``is_holding``, the
   silence threshold is ignored.

2. **End the recording when the key comes up.** On the hold_start -> hold_end
   transition we ``break`` and transcribe what we have — the same exit
   ``skip_forward`` already takes three lines above, so the downstream path is
   already proven.

Design notes
============
* The break fires only on a *transition*, tracked by ``_ptt_was_holding``. A
  stray hold_end with no hold in progress therefore cannot end a recording that
  PTT never started — conversational (silence-detection) mode is untouched when
  nothing ever sends hold_start.
* ``snap`` is already read once per loop iteration by upstream, so the hold
  check costs nothing extra and sees a consistent view.
* The stall backstop and max_duration bound still apply while holding: a held
  key cannot record forever, and a dead stream still ends in
  AUDIO_STALL_TIMEOUT. Holding suppresses *silence detection only*.
* Independent of patch_listen_overrun.py: that one rewrites the ``while (...)``
  header, these three anchors are the init line above it and two sites in the
  body. No overlap, so order does not matter.

Anchors verified against voice-mode 8.12.0 (2026-09-05).
Idempotent; fails loudly on drift.

Usage: patch_converse_hold.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local ptt hold"

# --- 1. transition tracker, initialised alongside the stall backstop --------
A_INIT = (
    "                AUDIO_STALL_TIMEOUT = 5.0\n"
    "                last_audio_time = time.monotonic()\n"
)
R_INIT = (
    "                AUDIO_STALL_TIMEOUT = 5.0\n"
    "                last_audio_time = time.monotonic()\n"
    "                # voicemode-local ptt hold: track the hold_start->hold_end\n"
    "                # edge so a stray key-up can't end a recording PTT never\n"
    "                # started. False forever when nothing sends hold_start, so\n"
    "                # conversational mode is byte-for-byte unaffected.\n"
    "                _ptt_was_holding = False\n"
)

# --- 2. end the recording when the key comes up ----------------------------
A_BREAK = (
    "                    if snap.is_skip_forward:\n"
    "                        logger.info(\"⏭  Recording ended early by skip_forward "
    "-- transcribing what we have\")\n"
    "                        break\n"
)
R_BREAK = (
    "                    if snap.is_skip_forward:\n"
    "                        logger.info(\"⏭  Recording ended early by skip_forward "
    "-- transcribing what we have\")\n"
    "                        break\n"
    "                    # voicemode-local ptt hold: level-triggered push-to-talk.\n"
    "                    # While the key is down we keep the mic open (the silence\n"
    "                    # exit below is suppressed); on release we end the turn\n"
    "                    # via the same path skip_forward uses.\n"
    "                    if getattr(snap, \"is_holding\", False):\n"
    "                        _ptt_was_holding = True\n"
    "                    elif _ptt_was_holding:\n"
    "                        logger.info(\"🎤 PTT hold released -- transcribing "
    "what we have\")\n"
    "                        break\n"
)

# --- 3. suppress the silence exit while holding ----------------------------
A_SILENCE = (
    "                                if recording_duration >= effective_min_duration "
    "and silence_duration_ms >= SILENCE_THRESHOLD_MS:\n"
)
R_SILENCE = (
    "                                # voicemode-local ptt hold: while the PTT key\n"
    "                                # is down, a pause mid-thought must NOT end the\n"
    "                                # turn -- that is the entire point of hold-to-\n"
    "                                # talk. max_duration and the stall backstop\n"
    "                                # still bound the recording.\n"
    "                                if (not getattr(snap, \"is_holding\", False)\n"
    "                                        and recording_duration >= effective_min_duration\n"
    "                                        and silence_duration_ms >= SILENCE_THRESHOLD_MS):\n"
)

EDITS = [
    ("hold transition tracker", A_INIT, R_INIT, 1),
    ("hold release break", A_BREAK, R_BREAK, 1),
    ("silence-exit suppression", A_SILENCE, R_SILENCE, 1),
]


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, anchor, _, expected in EDITS:
        count = src.count(anchor)
        if count != expected:
            print(
                f"ANCHOR DRIFT: '{name}' matched {count} times (expected "
                f"{expected}) in {target}. Upstream converse.py changed — "
                f"update patches/patch_converse_hold.py.",
                file=sys.stderr,
            )
            return 1
    out = src
    for _, anchor, repl, expected in EDITS:
        out = out.replace(anchor, repl, expected)
    compile(out, str(target), "exec")  # syntax safety net before writing
    target.write_text(out)
    print(f"  patched (ptt hold): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
