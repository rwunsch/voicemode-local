#!/usr/bin/env python3
"""Audio-device calls off the event loop with a deadline, and a loop watchdog (FELIX #2095).

The logic is in voice_mode/audio_deadline.py (copied by the manifest); this script wires it in:

core.py
  - the three chimes and system audio: ``player.play(..., blocking=True)`` waited for the
    end of playback with no timeout, on the event loop. Now ``await audio_deadline.play``.
tools/converse.py
  - the cached-audio replay: the same call.
  - ``sd._terminate(); sd._initialize()`` at the start of every converse runs on a thread
    with a deadline.
  - an outermost ``_converse_core`` wrapper starts the watchdog, returns the "audio device
    did not respond" message (and schedules the exit) when the process is wedged, and runs
    the turn under ``audio_deadline.guard``, which ends it as soon as a miss marks the
    process wedged (upstream swallows a chime's miss and then waits with no deadline).
control_socket.py
  - the status reply carries ``loop_lag_s``; the pill shows "not responding" from it.

Idempotent (marker per file). Exits 1 naming the anchor when voice-mode no longer matches.
Verified against voice-mode 8.12.0 with the steps before it in manifest.json.

Usage: patch_audio_deadline.py <voice_mode/core.py> [<tools/converse.py>] [<control_socket.py>]
(any order; the file name decides what is done to it)
"""
import sys
from pathlib import Path

MARKER = "voicemode-local audio deadline"

CORE = [
    ("chimes", "        player.play(chime_float, sample_rate, blocking=True)\n",
     f"        await _vml_audio_deadline.play(player, chime_float, sample_rate)  # {MARKER}\n", 3),
    ("system audio", "            player.play(samples, audio.frame_rate, blocking=True)\n",
     f"            await _vml_audio_deadline.play(player, samples, audio.frame_rate)  # {MARKER}\n", 1),
    ("import", "from .audio_player import NonBlockingAudioPlayer\n",
     "from .audio_player import NonBlockingAudioPlayer\n"
     f"from . import audio_deadline as _vml_audio_deadline  # {MARKER}\n", 1),
]

WRAPPER = f'''

# {MARKER} (FELIX #2095): the outermost converse wrapper. A process whose audio device
# missed a deadline is not trusted: every converse says so and the process exits shortly
# after, so a fresh one starts. See voice_mode/audio_deadline.py.
_vml_converse_core_before_deadline = _converse_core


async def _converse_core(*args, **kwargs):
    from .. import audio_deadline
    try:
        audio_deadline.ensure_started()
    except Exception:
        logger.warning("audio deadline: could not start the loop watchdog", exc_info=True)
    wedged = audio_deadline.wedged_result()
    if wedged:
        return wedged
    # guard ends the turn as soon as a device call inside misses its deadline, even where
    # upstream swallows the miss and then waits with no deadline (TTS playback).
    return await audio_deadline.guard(_vml_converse_core_before_deadline(*args, **kwargs))
'''

CONVERSE = [
    ("cached-audio replay", "                                player.play(data, samplerate, blocking=True)\n",
     "                                from .. import audio_deadline as _vml_audio_deadline\n"
     f"                                await _vml_audio_deadline.play(player, data, samplerate)  # {MARKER}\n", 1),
    ("device refresh",
     "    # This takes ~1ms and ensures we use the current default device\n"
     "    import sounddevice as sd\n"
     "    sd._terminate()\n"
     "    sd._initialize()\n",
     "    # This takes ~1ms and ensures we use the current default device\n"
     "    import sounddevice as sd\n"
     f"    # {MARKER}: on a thread with a deadline; a miss raises DeadlineMissed, which the\n"
     "    # outermost _converse_core wrapper turns into the 'audio device did not respond' reply.\n"
     "    from .. import audio_deadline as _vml_audio_deadline\n"
     "    await _vml_audio_deadline.call(lambda: (sd._terminate(), sd._initialize()),\n"
     "                                   _vml_audio_deadline.deadline_slack(), \"refreshing the audio devices\")\n", 1),
]

SOCKET = [
    ("status reply",
     '        "listen_started_at": control_state.listen_started_at,\n',
     '        "listen_started_at": control_state.listen_started_at,\n'
     f'        "loop_lag_s": _vml_loop_lag(),  # {MARKER}\n', 1),
    ("helper", "class ControlSocketListener:\n",
     f"def _vml_loop_lag():  # {MARKER}\n"
     '    """Seconds since the voice server\'s event loop last turned (None if unknown). This reply\n'
     "    is built on a socket thread, so it still answers when the loop is stuck.\"\"\"\n"
     "    try:\n"
     "        from . import audio_deadline\n"
     "        return audio_deadline.loop_lag()\n"
     "    except Exception:\n"
     "        return None\n"
     "\n"
     "\n"
     "class ControlSocketListener:\n", 1),
]


def _apply(target: Path, edits, append: str = "") -> int:
    source = target.read_bytes().decode("utf-8")
    if MARKER in source:
        return 0
    ending = "\r\n" if "\r\n" in source else "\n"
    text = source.replace("\r\n", "\n")
    for name, old, new, count in edits:
        found = text.count(old)
        if found != count:
            print(f"{target}: anchor '{name}' found {found} times, expected {count}; "
                  "voice-mode has changed", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    text += append
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))
    return 0


def apply(target: Path) -> int:
    name = target.name
    if name == "core.py":
        return _apply(target, CORE)
    if name == "converse.py":
        return _apply(target, CONVERSE, WRAPPER)
    if name == "control_socket.py":
        return _apply(target, SOCKET)
    print(f"{target}: not a file this patch knows", file=sys.stderr)
    return 1


if __name__ == "__main__":
    rc = 0
    for arg in sys.argv[1:]:
        rc = apply(Path(arg)) or rc
    sys.exit(rc)
