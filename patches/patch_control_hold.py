#!/usr/bin/env python3
"""Add a level-triggered PTT *hold* to voice_mode/control_channel.py.

Why this exists
===============
Upstream's control channel (VM-1676/VM-1685/VM-1739) already gives us almost
all of push-to-talk:

  * ``skip_forward`` pressed while the assistant speaks cuts the utterance and
    hands over the mic  -> press-to-barge-in, done.
  * ``skip_forward`` pressed while *you* speak ends the recording immediately
    and transcribes       -> short-press-to-end-turn, done.

What is missing is the *hold* semantic: the mic open exactly while the key is
DOWN, with silence detection suppressed for the duration so a pause mid-thought
does not end the turn. That is the whole point of PTT — think, then push and
talk — and ``skip_forward`` cannot express it because it is edge-triggered.

Design
======
Hold is deliberately NOT another ``_state`` value. ``ControlState._state`` is a
single mutually-exclusive play/hold/cut field (running / paused / stopped /
skip_forward); hold is a *modifier on the recording phase*, orthogonal to all
of them — you can be holding while the state is running, and a stop must still
dominate. So it gets its own boolean, guarded by the same condition variable,
and surfaced as ``ControlSnapshot.is_holding``.

Everything mirrors the existing ``request_skip_forward`` / ``is_skip_forward``
pair so the shape is familiar to an upstream reviewer. ``reset()`` clears it,
like every other latched field, so a hold cannot leak into the next utterance.

Paired with patch_converse_hold.py, which consumes ``is_holding`` in the
recording loop. This patch alone is inert: nothing reads the flag.

Anchors verified against voice-mode 8.12.0 (2026-09-05).
Idempotent; fails loudly on drift.

Usage: patch_control_hold.py [<path-to-control_channel.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local ptt hold"

# --- 1. command constants + allowlist ---------------------------------------
A_CMDS = (
    "VALID_COMMANDS = (COMMAND_PAUSE, COMMAND_RESUME, COMMAND_STOP, "
    "COMMAND_SKIP_FORWARD, COMMAND_SKIP_BACK)\n"
)
R_CMDS = (
    "# voicemode-local ptt hold: level-triggered push-to-talk. Unlike the\n"
    "# edge-triggered skip_forward, a hold spans an interval -- the mic stays\n"
    "# open, and silence detection is suppressed, for exactly as long as the\n"
    "# key is down. hold_start/hold_end bracket that interval.\n"
    "COMMAND_HOLD_START = \"hold_start\"\n"
    "COMMAND_HOLD_END = \"hold_end\"\n"
    "\n"
    "VALID_COMMANDS = (COMMAND_PAUSE, COMMAND_RESUME, COMMAND_STOP, "
    "COMMAND_SKIP_FORWARD, COMMAND_SKIP_BACK,\n"
    "                  COMMAND_HOLD_START, COMMAND_HOLD_END)\n"
)

# --- 2. snapshot field ------------------------------------------------------
A_SNAP_FIELDS = (
    "    state: str\n"
    "    message: Optional[str] = None\n"
    "    hint: Optional[str] = None\n"
    "    pending_transport: Optional[str] = None\n"
)
R_SNAP_FIELDS = (
    "    state: str\n"
    "    message: Optional[str] = None\n"
    "    hint: Optional[str] = None\n"
    "    pending_transport: Optional[str] = None\n"
    "    # voicemode-local ptt hold: True while the PTT key is held down.\n"
    "    # Orthogonal to `state` -- you can be holding while running, and a\n"
    "    # stop still dominates. Consumed by the recording loop.\n"
    "    is_holding: bool = False\n"
)

# --- 3. ControlState.__init__ ----------------------------------------------
A_INIT = (
    "        self._pending_transport: Optional[str] = None\n"
    "\n"
    "    # --- mutations (listener side) ---------------------------------------\n"
)
R_INIT = (
    "        self._pending_transport: Optional[str] = None\n"
    "        # voicemode-local ptt hold: level-triggered, latched until hold_end\n"
    "        # or reset(). Not part of _state -- see patch_control_hold.py.\n"
    "        self._holding: bool = False\n"
    "\n"
    "    # --- mutations (listener side) ---------------------------------------\n"
)

# --- 4. request_hold_start / request_hold_end -------------------------------
A_RESET = (
    "    def reset(self) -> None:\n"
    "        \"\"\"Clear back to *running*; drop any latched message / hint / "
    "transport request.\n"
)
R_RESET = (
    "    def request_hold_start(self) -> bool:\n"
    "        \"\"\"Begin a PTT hold: keep the mic open until ``request_hold_end``.\n"
    "\n"
    "        voicemode-local ptt hold. While holding, the recording loop\n"
    "        suppresses its silence-detection exit, so a pause mid-thought does\n"
    "        not end the turn. Idempotent (a key-repeat is harmless). Refused\n"
    "        once stopped -- ``stop`` is the harder terminal, exactly as it\n"
    "        dominates ``skip_forward``.\n"
    "        \"\"\"\n"
    "        with self._cond:\n"
    "            if self._state == STATE_STOPPED:\n"
    "                logger.debug(\"hold_start ignored -- already stopped\")\n"
    "                return False\n"
    "            self._holding = True\n"
    "            self._cond.notify_all()\n"
    "            return True\n"
    "\n"
    "    def request_hold_end(self) -> bool:\n"
    "        \"\"\"End a PTT hold: the recording loop stops and transcribes.\n"
    "\n"
    "        voicemode-local ptt hold. Idempotent; returns False if no hold was\n"
    "        active, so a stray key-up cannot end a recording nobody started.\n"
    "        \"\"\"\n"
    "        with self._cond:\n"
    "            if not self._holding:\n"
    "                return False\n"
    "            self._holding = False\n"
    "            self._cond.notify_all()\n"
    "            return True\n"
    "\n"
    "    def reset(self) -> None:\n"
    "        \"\"\"Clear back to *running*; drop any latched message / hint / "
    "transport request.\n"
)

# --- 5. reset() clears the flag ---------------------------------------------
A_RESET_BODY = (
    "            self._state = STATE_RUNNING\n"
    "            self._message = None\n"
    "            self._hint = None\n"
    "            self._pending_transport = None\n"
    "            self._cond.notify_all()\n"
)
R_RESET_BODY = (
    "            self._state = STATE_RUNNING\n"
    "            self._message = None\n"
    "            self._hint = None\n"
    "            self._pending_transport = None\n"
    "            self._holding = False  # voicemode-local ptt hold\n"
    "            self._cond.notify_all()\n"
)

# --- 6. snapshot() carries it ----------------------------------------------
A_SNAP = (
    "            return ControlSnapshot(\n"
    "                self._state, self._message, self._hint, self._pending_transport\n"
    "            )\n"
)
R_SNAP = (
    "            return ControlSnapshot(\n"
    "                self._state, self._message, self._hint, self._pending_transport,\n"
    "                self._holding,  # voicemode-local ptt hold\n"
    "            )\n"
)

# (name, anchor, replacement, expected_occurrences)
# ControlSnapshot(...) is constructed TWICE -- in snapshot() and in
# wait_while_paused() -- and both must carry the hold flag, so that edit
# expects 2 and replaces both.
EDITS = [
    ("command allowlist", A_CMDS, R_CMDS, 1),
    ("snapshot fields", A_SNAP_FIELDS, R_SNAP_FIELDS, 1),
    ("ControlState.__init__", A_INIT, R_INIT, 1),
    ("hold request methods", A_RESET, R_RESET, 1),
    ("reset body", A_RESET_BODY, R_RESET_BODY, 1),
    ("snapshot construction", A_SNAP, R_SNAP, 2),
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
                f"{expected}) in {target}. Upstream control_channel.py changed "
                f"— update patches/patch_control_hold.py.",
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
        target = Path(voice_mode.__file__).parent / "control_channel.py"
    sys.exit(apply(target))
