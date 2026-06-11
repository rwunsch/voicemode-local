#!/usr/bin/env python3
"""Patch voice_mode/tools/converse.py so max_duration never truncates speech.

Upstream's record_audio_with_silence_detection treats max_duration
(= listen_duration_max, 60-120s as chosen by the calling agent) as a HARD
ceiling: `while recording_duration < max_duration and not stop_recording`.
A user still talking at the cap is cut off mid-sentence (2026-06-11:
recordings ended at exactly 60.0s/120.0s of samples, transcripts ended
mid-word — same bug class as the queue LISTEN_CAP truncation fixed the same
day, one layer deeper; it bites solo sessions too).

The fix: once speech has been detected, the listen window extends past
max_duration until the normal silence exit ends the recording. A safety
ceiling of max_duration + VOICEMODE_LISTEN_OVERRUN (seconds, default 300)
bounds the extension so a VAD stuck reporting speech (constant background
noise, system-audio fold-in) cannot record forever. Overrun 0 restores the
upstream hard cap. A silent window (speech never started) is still capped at
max_duration — and under queue contention the no_speech_timeout from
patch_converse_queue.py ends it even earlier.

Single exact-string anchor (must occur EXACTLY once, else exit 1 — an
upstream-drift detector). Idempotent: running on an already-patched file is
a no-op (exit 0). Order-independent of patch_converse_queue.py /
patch_converse_cancel.py — they do not touch the loop header line.

Anchor verified against voice-mode 8.7.1.

Usage: patch_listen_overrun.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local listen overrun"

A_LOOP = (
    "                while recording_duration < max_duration "
    "and not stop_recording:\n"
)
R_LOOP = (
    "                # voicemode-local listen overrun: upstream's max_duration\n"
    "                # is a hard ceiling, so a user still talking at the cap\n"
    "                # was cut off mid-sentence (2026-06-11, layer 2 — same\n"
    "                # class as the queue LISTEN_CAP bug). Once speech has\n"
    "                # started, listening extends until the normal silence\n"
    "                # exit, bounded by max_duration + VOICEMODE_LISTEN_OVERRUN\n"
    "                # (safety ceiling for a VAD stuck on background noise;\n"
    "                # 0 restores the upstream hard cap). A silent window is\n"
    "                # still capped at max_duration.\n"
    "                _overrun = max(0.0, float(\n"
    "                    os.getenv(\"VOICEMODE_LISTEN_OVERRUN\", \"300\") or 0))\n"
    "                _hard_max_duration = max_duration + _overrun\n"
    "                _overrun_logged = False\n"
    "                while not stop_recording and recording_duration < (\n"
    "                        _hard_max_duration if speech_detected else max_duration):\n"
    "                    if (speech_detected and not _overrun_logged\n"
    "                            and recording_duration >= max_duration):\n"
    "                        _overrun_logged = True\n"
    "                        logger.info(\n"
    "                            f\"🎤 User still speaking at max_duration \"\n"
    "                            f\"({max_duration:.0f}s) — extending listen \"\n"
    "                            f\"window up to {_hard_max_duration:.0f}s\")\n"
)


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"[patch_listen_overrun] {target}: already patched — skipping")
        return 0
    count = src.count(A_LOOP)
    if count != 1:
        print(f"[patch_listen_overrun] ERROR: recording-loop anchor matched "
              f"{count} times (expected exactly 1) in {target}.\n"
              f"Upstream voice-mode has likely changed — update the anchor "
              f"in patches/patch_listen_overrun.py.", file=sys.stderr)
        return 1
    out = src.replace(A_LOOP, R_LOOP)
    compile(out, str(target), "exec")  # syntax safety net before writing
    target.write_text(out)
    print(f"[patch_listen_overrun] patched {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
