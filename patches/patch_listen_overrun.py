#!/usr/bin/env python3
"""Patch voice_mode/tools/converse.py so max_duration never truncates speech.

Upstream's record_audio_with_silence_detection treats max_duration
(= listen_duration_max, 60-120s as chosen by the calling agent) as a HARD
ceiling. A user still talking at the cap is cut off mid-sentence (2026-06-11:
recordings ended at exactly 60.0s/120.0s of samples, transcripts ended
mid-word; it bites solo sessions too).

STILL PRESENT IN 8.12.0. Upstream added a *stall backstop* in 8.11.0
(AUDIO_STALL_TIMEOUT), but its own comment is explicit that this is not a
length cap:

    This is a dead-stream safety net, NOT a cap on recording length:
    last_audio_time is bumped on every chunk, so a healthy (even slow)
    recording is never truncated -- length is still governed by
    recording_duration < max_duration.

...which is exactly the bug. And converse.py:1559 confirms the intent: "The
only exit is speech detection or max_duration."

The fix: once speech has been detected, the listen window extends past
max_duration until the normal silence exit ends the recording. A safety
ceiling of max_duration + VOICEMODE_LISTEN_OVERRUN (seconds, default 300)
bounds the extension so a VAD stuck reporting speech (constant background
noise, system-audio fold-in) cannot record forever. Overrun 0 restores the
upstream hard cap. A silent window (speech never started) is still capped at
max_duration.

Upstream's stall backstop is PRESERVED verbatim in the new loop condition --
it is orthogonal (a dead stream still ends in AUDIO_STALL_TIMEOUT seconds
whether or not speech was detected) and is upstream's to own.

Single exact-string anchor spanning the two-line loop header (must occur
EXACTLY once, else exit 1 -- an upstream-drift detector). Idempotent: running
on an already-patched file is a no-op (exit 0).

Anchors verified against voice-mode 8.12.0 (2026-09-05).

Drafted for upstream as docs/upstream/pr-listen-overrun.md.

Usage: patch_listen_overrun.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local listen overrun"

# 8.12.0 wraps the condition in parentheses and adds the 8.11 stall backstop.
A_LOOP = (
    "                while (recording_duration < max_duration and not stop_recording\n"
    "                       and time.monotonic() - last_audio_time < AUDIO_STALL_TIMEOUT):\n"
)

R_LOOP = (
    "                # voicemode-local listen overrun: upstream's max_duration is\n"
    "                # still a hard ceiling in 8.12.0, so a user talking at the cap\n"
    "                # is cut off mid-sentence. Upstream's own comment above says\n"
    "                # the 8.11 stall backstop is deliberately NOT a length cap, so\n"
    "                # this remains unfixed upstream. Once speech has started,\n"
    "                # listening extends until the normal silence exit, bounded by\n"
    "                # max_duration + VOICEMODE_LISTEN_OVERRUN (safety ceiling for a\n"
    "                # VAD stuck on background noise; 0 restores the upstream hard\n"
    "                # cap). A silent window is still capped at max_duration.\n"
    "                # NOTE: the AUDIO_STALL_TIMEOUT term below is upstream's dead-\n"
    "                # stream backstop, preserved verbatim -- it is orthogonal.\n"
    "                _overrun = max(0.0, float(\n"
    "                    os.getenv(\"VOICEMODE_LISTEN_OVERRUN\", \"300\") or 0))\n"
    "                _hard_max_duration = max_duration + _overrun\n"
    "                _overrun_logged = False\n"
    "                while (not stop_recording\n"
    "                       and recording_duration < (\n"
    "                           _hard_max_duration if speech_detected else max_duration)\n"
    "                       and time.monotonic() - last_audio_time < AUDIO_STALL_TIMEOUT):\n"
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
