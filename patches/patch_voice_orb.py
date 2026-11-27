#!/usr/bin/env python3
"""Expose measured audio and safe click-to-interrupt for the existing Voice Pill.

Apply after patch_voice_pill. Only the existing recording and playback paths
take measurements; the control channel carries scalars, never audio. A click
names the playback it saw so it cannot interrupt the next turn.
"""
import sys
from pathlib import Path

MARKER = "voicemode-local voice orb"

TARGETS = {
    'control_channel.py': [
        ('pill interrupt allowlist',
         'COMMAND_BARGE_IN)\n',
         "COMMAND_BARGE_IN, 'pill_interrupt')\n"),
        ('pill interrupt dispatch',
         '            state.request_barge_in()  # voicemode-local voice barge\n',
         '            state.request_barge_in()  # voicemode-local voice barge\n'
         "        elif self.command == 'pill_interrupt':\n"
         '            state.request_pill_interrupt(self.message)\n'),
        ('level clock',
         'import threading\n',
         'import threading\n'
         'import time\n'),
        ('level storage',
         '        self._speaking: bool = False  # voicemode-local voice barge\n'
         '        self._listening: bool = False  # voicemode-local voice pill\n',
         '        self._speaking: bool = False  # voicemode-local voice barge\n'
         '        self._listening: bool = False  # voicemode-local voice pill\n'
         '        self._pill_levels = {}\n'
         '        self._pill_playback_id = None\n'
         '        self._pill_recording_id = None\n'),
        ('clear playback level',
         '            self._speaking = speaking\n',
         '            self._speaking = speaking\n'
         '            self._pill_levels.pop("speaking", None)\n'
         '            self._pill_playback_id = str(time.monotonic_ns()) if speaking else None\n'),
        ('reset the recording level',
         '            self._listening = listening\n',
         '            self._listening = listening\n'
         '            self._pill_levels.pop("listening", None)\n'
         '            self._pill_recording_id = str(time.monotonic_ns()) if listening else None\n'),
        ('measure audio and guard clicks',
         '    def request_barge_in(self) -> bool:\n',
         '    def pill_level(self, phase, samples, full_scale=1):\n'
         '        # Take at most 256 samples; never wait for a lock in an audio callback.\n'
         "        generation = self._pill_playback_id if phase == 'speaking' else self._pill_recording_id\n"
         '        if generation is None:\n'
         '            return\n'
         '        now = time.monotonic()\n'
         '        previous = self._pill_levels.get(phase)\n'
         '        if previous and now - previous[0] < 0.04:\n'
         '            return\n'
         '        try:\n'
         '            if not samples.size:\n'
         '                return\n'
         '            flat = samples.reshape(-1)\n'
         "            part = flat[::max(1, (flat.size + 255) // 256)].astype('float32')\n"
         '            rms = float(part.dot(part) / part.size) ** 0.5 / full_scale\n'
         "            if not 0 <= rms < float('inf'):\n"
         '                return\n'
         '            level = round(min(1.0, rms), 4)\n'
         '        except Exception:\n'
         '            return  # an optional indicator must never interrupt audio\n'
         '        if self._cond.acquire(False):\n'
         '            try:\n'
         "                current = self._pill_playback_id if phase == 'speaking' else self._pill_recording_id\n"
         '                if current == generation and self._state == STATE_RUNNING:\n'
         '                    self._pill_levels[phase] = (now, level)\n'
         '            finally:\n'
         '                self._cond.release()\n'
         '\n'
         '    def pill_levels(self):\n'
         '        with self._cond:\n'
         '            if self._state != STATE_RUNNING:\n'
         '                self._pill_levels.clear()\n'
         '                return {}\n'
         '            now = time.monotonic()\n'
         '            return {phase: value for phase, (stamp, value) in self._pill_levels.items()\n'
         '                    if now - stamp <= 0.35}\n'
         '\n'
         '    @property\n'
         '    def pill_playback_id(self):\n'
         '        with self._cond:\n'
         '            if self._speaking and not self._listening and self._state == STATE_RUNNING:\n'
         '                return self._pill_playback_id\n'
         '\n'
         '    def request_pill_interrupt(self, playback_id):\n'
         '        # Unlike plain skip_forward, a late click cannot cut the next recording.\n'
         '        with self._cond:\n'
         '            if (not playback_id or playback_id != self._pill_playback_id or\n'
         '                    not self._speaking or self._listening or self._state != STATE_RUNNING):\n'
         '                return False\n'
         '            self._state = STATE_SKIP_FORWARD\n'
         '            self._pill_levels.clear()\n'
         '            self._cond.notify_all()\n'
         '            return True\n'
         '\n'
         '    def request_barge_in(self) -> bool:\n'),
    ],
    'control_socket.py': [
        ('playback metadata',
         '        "listening": control_state.is_listening,  # voicemode-local voice pill\n',
         '        "listening": control_state.is_listening,  # voicemode-local voice pill\n'
         '        "pill_levels": control_state.pill_levels(),\n'
         '        "pill_playback_id": control_state.pill_playback_id,\n'),
    ],
    'converse.py': [
        ('attach level measurement to controlled playback',
         '\n'
         '    player.play(samples, sample_rate, blocking=False)\n'
         '    try:\n',
         '\n'
         '    player.pill_control = control_state\n'
         '    player.play(samples, sample_rate, blocking=False)\n'
         '    try:\n'),
        ('listening level at chunk_flat = chunk.flatten()',
         '\n'
         '                        chunk_flat = chunk.flatten()\n',
         '\n'
         "                        get_control_state().pill_level('listening', chunk, 32768)\n"
         '                        chunk_flat = chunk.flatten()\n'),
    ],
    'streaming.py': [
        ('speaking level at stream.write(audio_array)',
         '\n'
         '            stream.write(audio_array)\n',
         '\n'
         "            state.pill_level('speaking', audio_array, 32768)\n"
         '            stream.write(audio_array)\n'),
        ('speaking level at stream.write(np.frombuffer(chunk, dtype=np.int16))',
         '\n'
         '        stream.write(np.frombuffer(chunk, dtype=np.int16))\n',
         '\n'
         "        control_state.pill_level('speaking', np.frombuffer(chunk, dtype=np.int16), 32768)\n"
         '        stream.write(np.frombuffer(chunk, dtype=np.int16))\n'),
        ('speaking level at stream.write(audio_array)',
         '\n'
         '                    stream.write(audio_array)\n',
         '\n'
         "                    get_control_state().pill_level('speaking', audio_array, 32768)\n"
         '                    stream.write(audio_array)\n'),
        ('speaking level at stream.write(audio_array)',
         '\n'
         '                stream.write(audio_array)\n',
         '\n'
         "                get_control_state().pill_level('speaking', audio_array, 32768)\n"
         '                stream.write(audio_array)\n'),
        ('speaking level at stream.write(samples)',
         '\n'
         '                            stream.write(samples)\n',
         '\n'
         "                            get_control_state().pill_level('speaking', samples, 1)\n"
         '                            stream.write(samples)\n'),
        ('speaking level at stream.write(samples)',
         '\n'
         '                    stream.write(samples)\n',
         '\n'
         "                    get_control_state().pill_level('speaking', samples, 1)\n"
         '                    stream.write(samples)\n'),
    ],
    'audio_player.py': [
        ('player measurement hook',
         '        self.playback_complete = threading.Event()\n',
         '        self.pill_control = None\n'
         '        self.playback_complete = threading.Event()\n'),
        ('measure submitted samples',
         '            chunk_len = len(chunk)\n',
         '            if self.pill_control is not None:\n'
         "                self.pill_control.pill_level('speaking', chunk)\n"
         '            chunk_len = len(chunk)\n'),
    ],
    'core.py': [
        ('attach level measurement to buffered TTS',
         '                        player.play(samples_with_buffer, audio.frame_rate, blocking=False)\n',
         '                        player.pill_control = get_pill_control_state()\n'
         '                        player.play(samples_with_buffer, audio.frame_rate, blocking=False)\n'),
        ('control import',
         '\n'
         'import numpy as np\n',
         '\n'
         'import numpy as np\n'
         'from .control_channel import get_control_state as get_pill_control_state\n'),
    ],
}


def _read(target: Path):
    """The file as LF text, and whether it used CRLF. UTF-8 whatever the locale."""
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    """Write UTF-8 in the file's own line endings."""
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply(target: Path) -> int:
    edits = TARGETS.get(target.name)
    if edits is None:
        print(f"[patch_voice_orb] ERROR: unexpected target {target.name}", file=sys.stderr)
        return 1
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, anchor, _ in edits:
        count = src.count(anchor)
        if count != 1:
            print(f"ANCHOR DRIFT: '{name}' matched {count} times (expected 1) in {target}. "
                  "Update patches/patch_voice_orb.py.", file=sys.stderr)
            return 1
    out = src
    for _, anchor, repl in edits:
        out = out.replace(anchor, repl, 1)
    out += f"\n# {MARKER}\n"
    compile(out, str(target), "exec")
    _write(target, out, crlf)
    print(f"  patched (voice orb): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: patch_voice_orb.py <file>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
