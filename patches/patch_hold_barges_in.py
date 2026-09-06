#!/usr/bin/env python3
"""Make ``hold_start`` abort TTS playback, so PTT is a single command.

Why this replaced the barge-in-then-hold sequence
=================================================
The PTT client used to decide whether to barge in by asking the control
channel's ``status`` query whether audio was playing, then sending
``skip_forward`` followed by ``hold_start``. That premise is false. Upstream's
own docstring for ``build_status_payload`` says:

    ``now_playing`` is the most-recently-*rendered* utterance ... during active
    playback this is the previous completed utterance, not the live in-flight
    stream

and ``state`` is ``running`` whether the assistant is speaking or listening. So
**there is no way to detect active playback from the status query**, and every
live test mis-timed its hold as a result.

Sending ``skip_forward`` unconditionally is not an option either: with nothing
playing it latches ``STATE_SKIP_FORWARD``, which the recording loop reads as
"end this turn now".

So the hold itself becomes the barge-in. A press during playback aborts the
utterance and holds the mic; a press during the listen phase just holds the mic.
One command, no status query, no ordering to get wrong, and no interaction with
the ``skip_forward`` edge-consume.

Two poll sites, matching how ``is_stopped`` / ``is_skip_forward`` are already
handled at each:

* ``core.py::_wait_for_player_with_control`` -- the buffered player path.
* ``converse.py::_play_samples_controllable`` -- the streaming path.

Both already abort on ``is_stopped``; a hold now aborts the same way. Playback
teardown is upstream's, untouched -- this only adds a condition.

Anchors verified against voice-mode 8.12.0 (2026-09-06).
Idempotent; fails loudly on drift.

Usage: patch_hold_barges_in.py <core.py|converse.py>
"""
import sys
from pathlib import Path

MARKER = "voicemode-local hold barge-in"

# --- core.py: buffered player -----------------------------------------------
A_CORE = (
    "        if snap.is_stopped or snap.is_skip_forward:\n"
    "            logger.info(\"Buffered TTS playback stopped via control channel\")\n"
)
R_CORE = (
    "        # voicemode-local hold barge-in: a PTT press during playback holds\n"
    "        # the mic; cut the utterance so the user can answer immediately.\n"
    "        # There is no reliable way for a client to know playback is live\n"
    "        # (status' now_playing is the previous COMPLETED utterance), so the\n"
    "        # hold has to be self-sufficient rather than paired with a\n"
    "        # skip_forward the client guesses at.\n"
    "        if snap.is_stopped or snap.is_skip_forward or getattr(snap, \"is_holding\", False):\n"
    "            logger.info(\"Buffered TTS playback stopped via control channel\")\n"
)

# --- converse.py: streaming player ------------------------------------------
A_CONVERSE = (
    "        while not player.playback_complete.is_set():\n"
    "            snap = control_state.snapshot()\n"
    "            if snap.is_stopped:\n"
)
R_CONVERSE = (
    "        while not player.playback_complete.is_set():\n"
    "            snap = control_state.snapshot()\n"
    "            # voicemode-local hold barge-in: see patch_hold_barges_in.py.\n"
    "            # Treated like skip_forward (cut and advance to the mic), NOT\n"
    "            # like stop (which ends the turn) -- the user is holding the key\n"
    "            # because they want to talk.\n"
    "            if getattr(snap, \"is_holding\", False):\n"
    "                logger.info(\"TTS cut by PTT hold -- handing over the mic\")\n"
    "                player.stop()\n"
    "                return \"skip_forward\"\n"
    "            if snap.is_stopped:\n"
)

TARGETS = {
    "core.py": [("buffered playback abort", A_CORE, R_CORE, 1)],
    "converse.py": [("streaming playback abort", A_CONVERSE, R_CONVERSE, 1)],
}


def apply(target: Path) -> int:
    edits = TARGETS.get(target.name)
    if edits is None:
        print(f"[patch_hold_barges_in] ERROR: unexpected target {target.name}",
              file=sys.stderr)
        return 1
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, anchor, _, expected in edits:
        count = src.count(anchor)
        if count != expected:
            print(
                f"ANCHOR DRIFT: '{name}' matched {count} times (expected "
                f"{expected}) in {target}. Update patches/patch_hold_barges_in.py.",
                file=sys.stderr,
            )
            return 1
    out = src
    for _, anchor, repl, expected in edits:
        out = out.replace(anchor, repl, expected)
    compile(out, str(target), "exec")
    target.write_text(out)
    print(f"  patched (hold barge-in): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: patch_hold_barges_in.py <path to core.py or converse.py>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
