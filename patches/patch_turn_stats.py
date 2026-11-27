#!/usr/bin/env python3
"""Per-turn numbers on RECORDING_END, without audio or text (FELIX #2107).

The logic is in voice_mode/turn_stats.py (copied by the manifest); this wires it into
tools/converse.py:
  - record_audio_with_silence_detection_inner starts a TurnStats, feeds it every frame's
    webrtcvad decision and level, and names how the loop ended at each exit (silence, Done =
    skip_forward, push-to-talk release, the cap, no speech, stop, pause, skip_back, cancel,
    a stalled stream, a device error, an error). The two recordings without silence detection
    report end_reason "no_vad".
  - the RECORDING_END event gains turn_stats.FIELDS, the same keys every time.
Nothing is recorded or kept beyond those numbers. Counting never breaks a recording: the
per-frame call is guarded.

Runs after every other converse.py patch in the manifest (its anchors include their text).
Idempotent (marker). Exits 1 naming the anchor when voice-mode no longer matches.
Verified against voice-mode 8.12.0 with the steps before it in manifest.json and
manifest.windows.json.

Usage: patch_turn_stats.py <voice_mode/tools/converse.py>
"""
import sys
from pathlib import Path

MARKER = "voicemode-local turn stats"


def _after(line: str, code: str) -> str:
    """`line` (a full line with its newline) followed by `code` at the same indentation."""
    indent = line[: len(line) - len(line.lstrip(" "))]
    return line + f"{indent}{code}  # {MARKER}\n"


def _ended(line, reason):
    return (f"end: {reason}", line, _after(line, f'_vml_ts.end("{reason}")'), 1)


EDITS = [
    ("start",
     "        speech_detected = False\n        stop_recording = False\n",
     "        speech_detected = False\n        stop_recording = False\n"
     f"        from .. import turn_stats as _vml_turn_stats  # {MARKER}\n"
     f"        _vml_ts = _vml_turn_stats.start()  # {MARKER}\n", 1),
    ("frame",
     "                        # State machine for speech detection\n",
     f"                        try:  # {MARKER}: never let counting break the recording\n"
     "                            _vml_ts.silence_needed_ms = _vml_silence_threshold_ms\n"
     "                            _vml_ts.frame(is_speech, float(np.sqrt(np.mean(chunk_flat.astype(np.float32) ** 2))))\n"
     "                        except Exception:\n"
     "                            pass\n"
     "                        # State machine for speech detection\n", 1),
    ("end: silence", "                                    stop_recording = True\n",
     f'                                    _vml_ts.end("silence")  # {MARKER}\n'
     "                                    stop_recording = True\n", 1),
    _ended('                        logger.info("🛑 Recording stopped: caller was cancelled")\n', "cancel"),
    _ended('                        logger.info("🛑 Recording stopped via control channel")\n', "stop"),
    _ended('                        logger.info("Recording ended by Pause -- discarding it")\n', "pause"),
    _ended('                        logger.info("⏮  Recording ended early by skip_back -- handing off to replay")\n', "skip_back"),
    _ended('                        logger.info("⏭  Recording ended early by skip_forward -- transcribing what we have")\n', "done"),
    _ended('                        logger.info("🎤 PTT hold released -- transcribing what we have")\n', "ptt_release"),
    ("end: device_error",
     '                            logger.error("Audio device error detected - stopping recording")\n',
     '                            logger.error("Audio device error detected - stopping recording")\n'
     f'                            _vml_ts.end("device_error"); _vml_turn_stats.finish(_vml_ts)  # {MARKER}\n', 1),
    _ended('                        logger.error(f"Error processing audio chunk: {e}")\n', "error"),
    ("end: loop condition",
     "            # Concatenate all chunks\n",
     f"            # {MARKER}: the loop condition ended it when no branch said why\n"
     "            if _vml_ts.reason is None:\n"
     "                if time.monotonic() - last_audio_time >= AUDIO_STALL_TIMEOUT:\n"
     '                    _vml_ts.end("stall")\n'
     "                else:\n"
     '                    _vml_ts.end("cap" if speech_detected else "no_speech")\n'
     "            _vml_turn_stats.finish(_vml_ts)\n"
     "            # Concatenate all chunks\n", 1),
    ("no silence detection",
     "        return (record_audio(max_duration), True)\n",
     f"        from .. import turn_stats as _vml_turn_stats; _vml_turn_stats.no_vad()  # {MARKER}\n"
     "        return (record_audio(max_duration), True)\n", 2),
    ("event",
     '            "samples": len(audio_data)\n',
     '            "samples": len(audio_data),\n'
     f"            **_vml_turn_stats_fields(),  # {MARKER}\n", 1),
]

HELPER = f'''

def _vml_turn_stats_fields():  # {MARKER}
    """turn_stats.FIELDS for RECORDING_END; never fails the turn."""
    try:
        from .. import turn_stats
        return turn_stats.event_fields()
    except Exception:
        return {{}}
'''


def apply(target: Path) -> int:
    source = target.read_bytes().decode("utf-8")
    if MARKER in source:
        return 0
    ending = "\r\n" if "\r\n" in source else "\n"
    text = source.replace("\r\n", "\n")
    for name, old, new, count in EDITS:
        found = text.count(old)
        if found != count:
            print(f"{target}: anchor '{name}' found {found} times, expected {count}; "
                  "voice-mode has changed", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    text += HELPER
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or Path(sys.argv[1]).name != "converse.py":
        print("usage: patch_turn_stats.py <voice_mode/tools/converse.py>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
