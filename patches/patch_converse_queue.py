#!/usr/bin/env python3
"""Surgically patch voice_mode/tools/converse.py to use the session queue.

Usage: patch_converse_queue.py <path-to-converse.py>

Replaces the single-lock `conch` arbitration with our multi-session FIFO queue.
Most edits are exact-string replacements (each anchor must occur EXACTLY once,
else exit 1 naming the anchor — an upstream-drift detector). The conch
arbitration block is large and changes often upstream, so it is replaced
between two short, stable marker lines instead of as one giant exact anchor.
Running on an already-patched file is a no-op (exit 0).

Anchors verified against voice-mode 8.7.1.
"""
import sys
from pathlib import Path

MARKER = "voicemode-local session queue"

# ---- anchor: import (after the conch import) ----
A_IMPORT = "from voice_mode.conch import Conch\n"
R_IMPORT = (
    "from voice_mode.conch import Conch\n"
    "from voice_mode import voice_queue\n"
)

# ---- anchor: signature (insert our params before the closing `) -> str:`) ----
A_SIG = "    ref_text: Optional[str] = None,\n) -> str:"
R_SIG = (
    "    ref_text: Optional[str] = None,\n"
    "    ticket: Optional[str] = None,\n"
    "    end_burst: Union[bool, str] = False,\n"
    ") -> str:"
)

# ---- anchor: docstring parameter docs (after wait_for_conch docs) ----
A_DOC = (
    "• wait_for_conch (bool, default: false): Multi-agent coordination\n"
    "  - false: If another agent is speaking, return status immediately\n"
    "  - true: Wait until the other agent finishes, then speak"
)
R_DOC = (
    "• ticket (string): Session-queue ticket id from a previous QUEUED status.\n"
    "  Pass it back unchanged when re-calling after QUEUED — it preserves your\n"
    "  FIFO position. Omit it for a fresh question.\n"
    "• end_burst (bool, default: false): Set true on the FINAL exchange of a\n"
    "  conversation burst to hand the voice floor to the next waiting session.\n"
    "• wait_for_conch (bool, default: false): Multi-agent coordination\n"
    "  - false: If another agent is speaking, return status immediately\n"
    "  - true: Wait until the other agent finishes, then speak"
)

# ---- anchor: string-bool conversion block ----
A_CONV = (
    "    if isinstance(wait_for_conch, str):\n"
    "        wait_for_conch = wait_for_conch.lower() in ('true', '1', 'yes', 'on')"
)
R_CONV = (
    "    if isinstance(wait_for_conch, str):\n"
    "        wait_for_conch = wait_for_conch.lower() in ('true', '1', 'yes', 'on')\n"
    "    if isinstance(end_burst, str):\n"
    "        end_burst = end_burst.lower() in ('true', '1', 'yes', 'on')"
)

# ---- anchor: conch construction line ----
A_CONSTRUCT = '    conch = Conch(agent_name="converse")  # Named for event logging'
R_CONSTRUCT = (
    "    # --- voicemode-local session queue "
    "(docs/superpowers/specs/2026-06-07-voice-session-queue-design.md) ---\n"
    "    queue_session = voice_queue.QueueSession(voice=voice)"
)

# ---- anchor: the conch release block in finally ----
A_RELEASE = '''        # Release the conch to signal voice conversation has ended
        if CONCH_ENABLED and conch._acquired:
            held_seconds = conch.release()
            if event_logger:
                event_logger.log_event("CONCH_RELEASE", {
                    "pid": os.getpid(),
                    "held_seconds": held_seconds
                })
        else:
            # Don't call release() when not acquired — it would delete the lock
            # file belonging to the agent that IS holding the conch, defeating
            # the flock coordination (they'd end up locking different inodes).
            pass'''
R_RELEASE = '''        # voicemode-local session queue: stop heartbeat; release floor on
        # end_burst, else a final heartbeat starts the inter-call grace window
        if voice_queue.QUEUE_ENABLED:
            await queue_session.finish(end_burst=end_burst)
            if event_logger:
                event_logger.log_event("QUEUE_FINISH", {
                    "pid": os.getpid(),
                    "end_burst": bool(end_burst)
                })'''

EXACT_PATCHES = [
    ("import", A_IMPORT, R_IMPORT),
    ("signature", A_SIG, R_SIG),
    ("docstring", A_DOC, R_DOC),
    ("bool-conversion", A_CONV, R_CONV),
    ("conch-construction", A_CONSTRUCT, R_CONSTRUCT),
    ("conch-release", A_RELEASE, R_RELEASE),
]

# ---- block: the whole conch arbitration block (between two stable markers) ----
# Replaced wholesale because upstream rewrites its internals frequently. The
# start marker is the first line of the block; the end marker is the first line
# AFTER it (the recording setup) — everything in between becomes our queue
# acquire. Preserves the upstream tmux auto-focus.
B_START = "        # Try to acquire conch atomically (no race condition)\n"
B_END = "        # Local microphone approach with timing\n"
B_REPLACE = '''        # voicemode-local session queue: FIFO arbitration replaces conch
        if voice_queue.QUEUE_ENABLED:
            _q = await queue_session.acquire(ticket=ticket)
            if _q.status == "queued":
                return _q.queued_message
            if _q.waited:
                # Handoff intro: announce which session is speaking now
                message = f"{queue_session.intro} {message}"
            queue_session.start_heartbeat()
            # When other sessions are queued, cap the listen window so a silent
            # holder yields the mic sooner instead of gripping it for the full
            # duration (VAD still ends recording early when the user speaks).
            listen_duration_max = queue_session.effective_listen_seconds(listen_duration_max)
            if event_logger:
                event_logger.log_event("QUEUE_ACQUIRE", {
                    "pid": os.getpid(),
                    "waited": _q.waited
                })
            # Auto-focus tmux pane after acquiring the floor, before playback.
            if AUTO_FOCUS_PANE and is_tmux():
                focus_tmux_pane()

'''


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text()

    if MARKER in text:
        print(f"[patch_converse_queue] {path}: already patched — skipping")
        return 0

    for name, anchor, _ in EXACT_PATCHES:
        count = text.count(anchor)
        if count != 1:
            print(f"[patch_converse_queue] ERROR: anchor '{name}' matched "
                  f"{count} times (expected exactly 1) in {path}.\n"
                  f"Upstream voice-mode has likely changed — update the anchors "
                  f"in patches/patch_converse_queue.py.", file=sys.stderr)
            return 1

    # Validate the arbitration block markers (start before end, each unique).
    for name, marker in (("arbitration-start", B_START), ("arbitration-end", B_END)):
        if text.count(marker) != 1:
            print(f"[patch_converse_queue] ERROR: block marker '{name}' matched "
                  f"{text.count(marker)} times (expected 1) in {path}.\n"
                  f"Upstream voice-mode has likely changed — update the markers "
                  f"in patches/patch_converse_queue.py.", file=sys.stderr)
            return 1
    si, ei = text.index(B_START), text.index(B_END)
    if not si < ei:
        print("[patch_converse_queue] ERROR: arbitration start marker is not "
              "before the end marker.", file=sys.stderr)
        return 1

    # Apply: block replacement first, then exact-string edits.
    text = text[:si] + B_REPLACE + text[ei:]
    for _, anchor, replacement in EXACT_PATCHES:
        text = text.replace(anchor, replacement)

    compile(text, str(path), "exec")  # syntax safety net before writing
    path.write_text(text)
    print(f"[patch_converse_queue] patched {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
