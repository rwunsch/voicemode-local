#!/usr/bin/env python3
"""Surgically patch voice_mode/tools/converse.py to use the session queue.

Usage: patch_converse_queue.py <path-to-converse.py>

Exact-string replacements, each anchor must occur EXACTLY once; otherwise we
exit 1 with a message naming the missing anchor (upstream drift detector).
Running on an already-patched file is a no-op (exit 0).
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

# ---- anchor: signature (insert params before wait_for_conch) ----
A_SIG = "    wait_for_conch: Union[bool, str] = False\n) -> str:"
R_SIG = (
    "    ticket: Optional[str] = None,\n"
    "    end_burst: Union[bool, str] = False,\n"
    "    wait_for_conch: Union[bool, str] = False\n) -> str:"
)

# ---- anchor: docstring parameter docs ----
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
    "• wait_for_conch: DEPRECATED — ignored while the session queue is enabled."
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

# ---- anchor: the whole conch arbitration block inside the try ----
A_ARBITRATE = '''        # Try to acquire conch atomically (no race condition)
        if CONCH_ENABLED:
            acquired = conch.try_acquire()

            if not acquired:
                # Another agent has the conch
                holder = Conch.get_holder()
                holder_agent = holder.get('agent', 'unknown') if holder else 'unknown'

                if event_logger:
                    event_logger.log_event("CONCH_BLOCKED", {
                        "pid": os.getpid(),
                        "holder_pid": holder.get('pid') if holder else None,
                        "holder_agent": holder_agent,
                        "wait_for_conch": wait_for_conch
                    })

                if not wait_for_conch:
                    # Default: return immediately with status info
                    return (f"User is currently speaking with {holder_agent}. "
                            "Use wait_for_conch=true to queue, or try again later.")

                # Wait mode - poll with atomic retry
                if event_logger:
                    event_logger.log_event("CONCH_WAIT_START", {
                        "pid": os.getpid(),
                        "holder_agent": holder_agent,
                        "timeout": CONCH_TIMEOUT
                    })

                waited = 0.0
                while not conch.try_acquire() and waited < CONCH_TIMEOUT:
                    await asyncio.sleep(CONCH_CHECK_INTERVAL)
                    waited += CONCH_CHECK_INTERVAL

                if event_logger:
                    event_logger.log_event("CONCH_WAIT_END", {
                        "pid": os.getpid(),
                        "waited_seconds": waited,
                        "result": "acquired" if conch._acquired else "timeout"
                    })

                if not conch._acquired:
                    return f"Timed out waiting for conch ({CONCH_TIMEOUT}s). {holder_agent} is still speaking."

            # Successfully acquired
            if event_logger:
                event_logger.log_event("CONCH_ACQUIRE", {
                    "pid": os.getpid(),
                    "agent": "converse"
                })'''
R_ARBITRATE = '''        # voicemode-local session queue: FIFO arbitration replaces conch
        if voice_queue.QUEUE_ENABLED:
            _q = await queue_session.acquire(ticket=ticket)
            if _q.status == "queued":
                return _q.queued_message
            if _q.waited:
                # Handoff intro: announce which session is speaking now
                message = f"{queue_session.intro} {message}"
            queue_session.start_heartbeat()
            if event_logger:
                event_logger.log_event("QUEUE_ACQUIRE", {
                    "pid": os.getpid(),
                    "waited": _q.waited
                })'''

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

PATCHES = [
    ("import", A_IMPORT, R_IMPORT),
    ("signature", A_SIG, R_SIG),
    ("docstring", A_DOC, R_DOC),
    ("bool-conversion", A_CONV, R_CONV),
    ("conch-construction", A_CONSTRUCT, R_CONSTRUCT),
    ("conch-arbitration", A_ARBITRATE, R_ARBITRATE),
    ("conch-release", A_RELEASE, R_RELEASE),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    text = path.read_text()

    if MARKER in text:
        print(f"[patch_converse_queue] {path}: already patched — skipping")
        return 0

    for name, anchor, _ in PATCHES:
        count = text.count(anchor)
        if count != 1:
            print(f"[patch_converse_queue] ERROR: anchor '{name}' matched "
                  f"{count} times (expected exactly 1) in {path}.\n"
                  f"Upstream voice-mode has likely changed — update the anchors "
                  f"in patches/patch_converse_queue.py.", file=sys.stderr)
            return 1

    for _, anchor, replacement in PATCHES:
        text = text.replace(anchor, replacement)

    compile(text, str(path), "exec")  # syntax safety net before writing
    path.write_text(text)
    print(f"[patch_converse_queue] patched {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
