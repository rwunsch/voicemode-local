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

# ---- anchors: no-speech timeout in the recording loop ----
# Under queue contention the holder must yield the mic when the user never
# starts speaking — WITHOUT truncating active speech. Capping
# listen_duration_max instead (a hard ceiling in the recording loop) was the
# 2026-06-11 bug that cut the user off mid-sentence at ~8s.
A_REC_SIG = (
    "def record_audio_with_silence_detection(max_duration: float, "
    "disable_silence_detection: bool = False, min_duration: float = 0.0, "
    "vad_aggressiveness: Optional[int] = None) -> Tuple[np.ndarray, bool]:"
)
R_REC_SIG = (
    "def record_audio_with_silence_detection(max_duration: float, "
    "disable_silence_detection: bool = False, min_duration: float = 0.0, "
    "vad_aggressiveness: Optional[int] = None, "
    "no_speech_timeout: Optional[float] = None) -> Tuple[np.ndarray, bool]:"
)

A_REC_DOC = (
    "        vad_aggressiveness: VAD aggressiveness level (0-3). "
    "If None, uses VAD_AGGRESSIVENESS from config\n"
)
R_REC_DOC = (
    "        vad_aggressiveness: VAD aggressiveness level (0-3). "
    "If None, uses VAD_AGGRESSIVENESS from config\n"
    "        no_speech_timeout: If set, stop after this many seconds ONLY if\n"
    "            speech never started (voicemode-local session queue contention);\n"
    "            never truncates active speech\n"
)

A_REC_WAIT = (
    "                            # No timeout in this state - just keep waiting\n"
    "                            # The only exit is speech detection or max_duration\n"
)
R_REC_WAIT = (
    "                            # voicemode-local session queue: sessions are\n"
    "                            # waiting and nobody has spoken yet — yield the\n"
    "                            # mic. Cannot fire once speech has started.\n"
    "                            elif no_speech_timeout is not None and recording_duration >= no_speech_timeout:\n"
    "                                logger.info(f\"No speech within {no_speech_timeout:.1f}s and sessions are waiting - yielding mic\")\n"
    "                                stop_recording = True\n"
)

A_REC_RETRY = (
    "                    return record_audio_with_silence_detection(max_duration, "
    "disable_silence_detection, min_duration, vad_aggressiveness)"
)
R_REC_RETRY = (
    "                    return record_audio_with_silence_detection(max_duration, "
    "disable_silence_detection, min_duration, vad_aggressiveness, no_speech_timeout)"
)

# All three recording call sites in converse() are textually identical, hence
# expected_count=3 (replaced everywhere).
A_REC_CALL = (
    "record_audio_with_silence_detection, listen_duration_max, "
    "disable_silence_detection, listen_duration_min, vad_aggressiveness\n"
)
R_REC_CALL = (
    "record_audio_with_silence_detection, listen_duration_max, "
    "disable_silence_detection, listen_duration_min, vad_aggressiveness, "
    "_queue_no_speech_timeout\n"
)

# (name, anchor, replacement, expected_count) — every occurrence is replaced.
EXACT_PATCHES = [
    ("import", A_IMPORT, R_IMPORT, 1),
    ("signature", A_SIG, R_SIG, 1),
    ("docstring", A_DOC, R_DOC, 1),
    ("bool-conversion", A_CONV, R_CONV, 1),
    ("conch-construction", A_CONSTRUCT, R_CONSTRUCT, 1),
    ("conch-release", A_RELEASE, R_RELEASE, 1),
    ("record-signature", A_REC_SIG, R_REC_SIG, 1),
    ("record-docstring", A_REC_DOC, R_REC_DOC, 1),
    ("record-waiting-timeout", A_REC_WAIT, R_REC_WAIT, 1),
    ("record-device-retry", A_REC_RETRY, R_REC_RETRY, 1),
    ("record-call-sites", A_REC_CALL, R_REC_CALL, 3),
]

# ---- block: the whole conch arbitration block (between two stable markers) ----
# Replaced wholesale because upstream rewrites its internals frequently. The
# start marker is the first line of the block; the end marker is the first line
# AFTER it (the recording setup) — everything in between becomes our queue
# acquire. Preserves the upstream tmux auto-focus.
B_START = "        # Try to acquire conch atomically (no race condition)\n"
B_END = "        # Local microphone approach with timing\n"
B_REPLACE = '''        # voicemode-local session queue: FIFO arbitration replaces conch
        _queue_no_speech_timeout = None
        if voice_queue.QUEUE_ENABLED:
            _q = await queue_session.acquire(ticket=ticket)
            if _q.status == "queued":
                return _q.queued_message
            if _q.waited:
                # Handoff intro: announce which session is speaking now
                message = f"{queue_session.intro} {message}"
            queue_session.start_heartbeat()
            # When other sessions are queued, stop listening after LISTEN_CAP
            # seconds ONLY if the user never starts speaking (no-speech
            # timeout). Active speech is never truncated; normal silence
            # detection ends the recording once the user has spoken.
            _queue_no_speech_timeout = queue_session.effective_no_speech_timeout()
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

    for name, anchor, _, expected in EXACT_PATCHES:
        count = text.count(anchor)
        if count != expected:
            print(f"[patch_converse_queue] ERROR: anchor '{name}' matched "
                  f"{count} times (expected exactly {expected}) in {path}.\n"
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
    for _, anchor, replacement, _expected in EXACT_PATCHES:
        text = text.replace(anchor, replacement)

    compile(text, str(path), "exec")  # syntax safety net before writing
    path.write_text(text)
    print(f"[patch_converse_queue] patched {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
