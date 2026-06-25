#!/usr/bin/env python3
"""Patch record_audio_with_silence_detection to bound the capture loop by a
WALL-CLOCK frame-arrival gap — the root fix for the issue-#5 recording hang.

The bug
=======
The VAD recording loop advances `recording_duration` only when a chunk is pulled
from the audio queue (recording_duration += chunk_duration_s, inside the try).
On `queue.Empty` it just `continue`s. So when the sounddevice capture callback
STARVES — no frames arriving, the WSLg audio-capture hang — recording_duration
FREEZES, the loop condition `recording_duration < max_duration` stays true
forever, and the recording never returns. The converse coroutine wedges (event
loop still alive, heartbeat still beating), holding the floor + audio device
until the queue watchdog hard-exits it ~200s later. Observed repeatedly under
multi-session load (every "coroutine wedged 200s" watchdog firing is this).

The fix
=======
Detect a real GAP in frame arrival: record the monotonic time of the last
received chunk; if no chunk arrives for VOICEMODE_AUDIO_STALL_GRACE seconds
(default 5), the callback has starved — end the recording. This CANNOT
false-fire on a healthy recording: even a silent listen keeps receiving frames
(silence is frames classified as non-speech, handled separately by the
no-speech timeout), and slow per-chunk processing doesn't matter because the
guard keys off frame *arrival*, not cumulative duration lag. Only true
device-level starvation trips it.

Two anchored, idempotent replacements; fails loudly on drift. Verified against
voice-mode 8.7.1 + patch_listen_overrun. Usage: patch_listen_stall.py [<converse.py>]
"""
import sys
from pathlib import Path

MARKER = "audio-stall guard (voicemode-local)"

A1_OLD = (
    "                _overrun_logged = False\n"
    "                while not stop_recording and recording_duration < (\n"
    "                        _hard_max_duration if speech_detected else max_duration):\n"
)
A1_NEW = (
    "                _overrun_logged = False\n"
    "                # audio-stall guard (voicemode-local): recording_duration only\n"
    "                # advances when a chunk arrives, so if the capture callback\n"
    "                # starves (WSLg audio hang, issue #5) it freezes and this loop\n"
    "                # never exits — the converse wedges until the queue watchdog\n"
    "                # hard-exits it ~200s later. Detect a real GAP in frame arrival\n"
    "                # and end the recording. A healthy recording (even a silent one)\n"
    "                # keeps receiving frames, so this only fires on device starvation.\n"
    "                _stall_grace = max(2.0, float(\n"
    "                    os.getenv(\"VOICEMODE_AUDIO_STALL_GRACE\", \"5\") or 0))\n"
    "                _last_chunk_at = time.monotonic()\n"
    "                while not stop_recording and recording_duration < (\n"
    "                        _hard_max_duration if speech_detected else max_duration):\n"
    "                    if time.monotonic() - _last_chunk_at >= _stall_grace:\n"
    "                        logger.warning(\n"
    "                            f\"Audio capture stalled: no frames for \"\n"
    "                            f\"{time.monotonic() - _last_chunk_at:.1f}s \"\n"
    "                            f\"(callback starved) — ending recording at \"\n"
    "                            f\"{recording_duration:.1f}s captured\")\n"
    "                        stop_recording = True\n"
    "                        break\n"
)

A2_OLD = (
    "                        # Get audio chunk from queue with timeout\n"
    "                        chunk = audio_queue.get(timeout=0.1)\n"
)
A2_NEW = (
    "                        # Get audio chunk from queue with timeout\n"
    "                        chunk = audio_queue.get(timeout=0.1)\n"
    "                        _last_chunk_at = time.monotonic()  # frame arrived (stall guard)\n"
)


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, anchor in (("loop-head", A1_OLD), ("queue-get", A2_OLD)):
        if src.count(anchor) != 1:
            print(f"ANCHOR DRIFT: '{name}' matched {src.count(anchor)} times "
                  f"(expected 1) in {target}. Upstream/patch_listen_overrun "
                  f"changed — update patch_listen_stall.py.", file=sys.stderr)
            return 1
    out = src.replace(A1_OLD, A1_NEW, 1).replace(A2_OLD, A2_NEW, 1)
    compile(out, str(target), "exec")
    target.write_text(out)
    print(f"  patched (audio-stall guard): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
