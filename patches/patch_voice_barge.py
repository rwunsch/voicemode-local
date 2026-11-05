#!/usr/bin/env python3
"""Give voice-mode the two things voice barge-in needs.

1. ``speaking``: whether TTS is playing RIGHT NOW, in the status reply. Upstream
   cannot say: ``state`` reads "running" while speaking and listening alike, and
   ``now_playing`` is the previous completed utterance. The mini UI needs this too.

2. ``barge_in``: skip_forward, but only if TTS is live. Plain skip_forward with
   nothing playing latches a state the recording loop reads as "end this turn",
   so a client deciding from a status poll would race playback ending. Checking
   and setting under the state lock removes the race.

3. A handoff so the first words survive. A cut by voice skips the pause and the
   chime (you are already talking), reports when the stream opened, treats that
   stream as speech already in progress, and prepends the trigger's audio.

Playback is bracketed by renaming each player function to ``<name>_inner`` and
putting a small try/finally wrapper in its place, so upstream's body is
untouched. Covers the buffered player and the turns[] player. patch_voice_pill
does the same for the chunked streaming path.

With the hold patches (patch_control_hold) present, control_channel.py is edited on
their output exactly as before; without them, on pristine 8.12 text. The hold marker
decides, so a hold-patched file whose anchors drifted fails rather than falling back.
The other three files never needed the hold patches. Anchors verified against
voice-mode 8.12.0 (2026-11-05), with and without the hold patches. Idempotent; fails
loudly on drift.

Usage: patch_voice_barge.py <control_channel.py|control_socket.py|core.py|converse.py>
"""
import sys
from pathlib import Path

MARKER = "voicemode-local voice barge"
HOLD_MARKER = "voicemode-local ptt hold"

SPEAKING_FIELDS = (
    f"        self._speaking: bool = False  # {MARKER}\n"
    "        self._barged: bool = False\n"
    "        self._listen_started_at = None\n"
)
SPEAKING_METHODS = (
    f"    # {MARKER}: live playback state, set by the players.\n"
    "    def set_speaking(self, speaking: bool) -> None:\n"
    "        with self._cond:\n"
    "            self._speaking = speaking\n"
    "\n"
    "    @property\n"
    "    def is_speaking(self) -> bool:\n"
    "        with self._cond:\n"
    "            return self._speaking\n"
    "\n"
    "    def request_barge_in(self) -> bool:\n"
    '        """Cut TTS and advance to listening, only if TTS is playing. Checked\n'
    "        under the lock, so it cannot land after playback ended and be read as\n"
    '        "end this turn". Stop still dominates."""\n'
    "        with self._cond:\n"
    "            if not self._speaking or self._state == STATE_STOPPED:\n"
    "                return False\n"
    "            self._state = STATE_SKIP_FORWARD\n"
    "            self._barged = True\n"
    "            self._listen_started_at = None\n"
    "            self._cond.notify_all()\n"
    "            return True\n"
    "\n"
    "    def take_barged(self) -> bool:\n"
    '        """Was the last cut made by voice? Separate from reset(), which runs\n'
    '        before the listen path needs the answer."""\n'
    "        with self._cond:\n"
    "            barged, self._barged = self._barged, False\n"
    "            return barged\n"
    "\n"
    "    def mark_listen_started(self, t: float) -> None:\n"
    "        with self._cond:\n"
    "            self._listen_started_at = t\n"
    "\n"
    "    @property\n"
    "    def listen_started_at(self):\n"
    "        with self._cond:\n"
    "            return self._listen_started_at\n"
    "\n"
)

BARGE_DISPATCH = (
    "        elif self.command == COMMAND_BARGE_IN:\n"
    f"            state.request_barge_in()  # {MARKER}\n"
)

# control_channel.py after patch_control_hold.
CHANNEL = [
    ("command constant",
     'COMMAND_HOLD_END = "hold_end"\n',
     'COMMAND_HOLD_END = "hold_end"\n'
     f'# {MARKER}: skip_forward, but a no-op unless TTS is playing.\n'
     'COMMAND_BARGE_IN = "barge_in"\n'),
    ("command allowlist",
     "                  COMMAND_HOLD_START, COMMAND_HOLD_END)\n",
     "                  COMMAND_HOLD_START, COMMAND_HOLD_END, COMMAND_BARGE_IN)\n"),
    ("command dispatch",
     "            state.request_hold_end()  # voicemode-local ptt hold\n",
     "            state.request_hold_end()  # voicemode-local ptt hold\n" + BARGE_DISPATCH),
    ("speaking field",
     "        self._holding: bool = False\n",
     "        self._holding: bool = False\n" + SPEAKING_FIELDS),
    ("speaking methods",
     "    def request_hold_start(self) -> bool:\n",
     SPEAKING_METHODS + "    def request_hold_start(self) -> bool:\n"),
]

# control_channel.py without the hold patches (pristine 8.12, or FELIX's tree).
PRISTINE_ALLOWLIST = ("VALID_COMMANDS = (COMMAND_PAUSE, COMMAND_RESUME, COMMAND_STOP, "
                      "COMMAND_SKIP_FORWARD, COMMAND_SKIP_BACK")
CHANNEL_NO_HOLD = [
    ("command constant and allowlist",
     PRISTINE_ALLOWLIST + ")\n",
     f"# {MARKER}: skip_forward, but a no-op unless TTS is playing.\n"
     'COMMAND_BARGE_IN = "barge_in"\n'
     "\n"
     + PRISTINE_ALLOWLIST + ",\n"
     "                  COMMAND_BARGE_IN)\n"),
    ("command dispatch",
     "        elif self.command == COMMAND_SKIP_FORWARD:\n"
     "            state.request_skip_forward()\n",
     "        elif self.command == COMMAND_SKIP_FORWARD:\n"
     "            state.request_skip_forward()\n" + BARGE_DISPATCH),
    ("speaking field",
     "        self._pending_transport: Optional[str] = None\n"
     "\n"
     "    # --- mutations (listener side) ---------------------------------------\n",
     "        self._pending_transport: Optional[str] = None\n" + SPEAKING_FIELDS +
     "\n"
     "    # --- mutations (listener side) ---------------------------------------\n"),
    ("speaking methods",
     "    def reset(self) -> None:\n",
     SPEAKING_METHODS + "    def reset(self) -> None:\n"),
]

SOCKET = [
    ("status field",
     '        "state": snap.state,\n',
     '        "state": snap.state,\n'
     f'        "speaking": control_state.is_speaking,  # {MARKER}\n'
     '        "listen_started_at": control_state.listen_started_at,\n'),
]

CORE = [
    ("buffered player",
     "async def _wait_for_player_with_control(player: NonBlockingAudioPlayer) -> bool:\n",
     f"# {MARKER}: report live playback for the length of the wait.\n"
     "async def _wait_for_player_with_control(player: NonBlockingAudioPlayer) -> bool:\n"
     "    from .control_channel import get_control_state\n"
     "\n"
     "    control_state = get_control_state()\n"
     "    control_state.set_speaking(True)\n"
     "    try:\n"
     "        return await _wait_for_player_with_control_inner(player)\n"
     "    finally:\n"
     "        control_state.set_speaking(False)\n"
     "\n"
     "\n"
     "async def _wait_for_player_with_control_inner(player: NonBlockingAudioPlayer) -> bool:\n"),
]

SIG = "(samples, sample_rate, control_state, poll_interval: float = 0.05) -> str:\n"
CONVERSE = [
    ("turns player",
     "async def _play_samples_controllable" + SIG,
     f"# {MARKER}: report live playback for the length of the call.\n"
     "async def _play_samples_controllable" + SIG +
     "    control_state.set_speaking(True)\n"
     "    try:\n"
     "        return await _play_samples_controllable_inner(samples, sample_rate, control_state, poll_interval)\n"
     "    finally:\n"
     "        control_state.set_speaking(False)\n"
     "\n"
     "\n"
     "async def _play_samples_controllable_inner" + SIG),
    ("pending flag",
     "def record_audio_with_silence_detection(",
     f"# {MARKER}: set when the next recording follows a cut by voice.\n"
     "_voice_barge_pending = threading.Event()\n"
     "\n"
     "\n"
     "def record_audio_with_silence_detection("),
    ("speech in progress",
     '                logger.debug("Started continuous audio stream")\n',
     '                logger.debug("Started continuous audio stream")\n'
     f"                # {MARKER}: the user is mid-sentence, so end on their next\n"
     "                # silence, and tell the trigger where its audio must stop.\n"
     "                if _voice_barge_pending.is_set():\n"
     "                    _voice_barge_pending.clear()\n"
     "                    speech_detected = True\n"
     "                    get_control_state().mark_listen_started(time.time())\n"),
    ("skip pause and chime",
     "    timings: Dict[str, float] = {}\n\n    if pre_listen_pause:\n",
     "    timings: Dict[str, float] = {}\n\n"
     f"    # {MARKER}: they are already talking; do not make them wait for a chime.\n"
     "    _barged = control_state.take_barged()\n"
     "    if _barged:\n"
     "        pre_listen_pause = 0.0\n"
     "        chime_enabled = False\n\n"
     "    if pre_listen_pause:\n"),
    ("arm recording",
     '    record_start = time.perf_counter()\n    logger.debug(f"About to call record_audio_with_silence_detection',
     "    if _barged:\n"
     "        _voice_barge_pending.set()\n"
     '    record_start = time.perf_counter()\n    logger.debug(f"About to call record_audio_with_silence_detection'),
    ("merge preroll",
     "    timings['record'] = time.perf_counter() - record_start\n\n    if event_logger:\n        event_logger.log_event(event_logger.RECORDING_END, {\n",
     "    timings['record'] = time.perf_counter() - record_start\n\n"
     "    if _barged:\n"
     "        _voice_barge_pending.clear()\n"
     "        try:\n"
     "            from voice_mode.voice_barge import prepend_preroll\n"
     "            audio_data = await asyncio.to_thread(\n"
     "                prepend_preroll, audio_data, control_state.listen_started_at, SAMPLE_RATE)\n"
     "        except Exception:  # a failed handoff must not lose the recording\n"
     '            logger.warning("voice barge: could not prepend preroll", exc_info=True)\n\n'
     "    if event_logger:\n        event_logger.log_event(event_logger.RECORDING_END, {\n"),
]

TARGETS = {"control_channel.py": CHANNEL, "control_socket.py": SOCKET,
           "core.py": CORE, "converse.py": CONVERSE}


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
        print(f"[patch_voice_barge] ERROR: unexpected target {target.name}", file=sys.stderr)
        return 1
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    if edits is CHANNEL and HOLD_MARKER not in src:
        edits = CHANNEL_NO_HOLD
    for name, anchor, _ in edits:
        count = src.count(anchor)
        if count != 1:
            print(f"ANCHOR DRIFT: '{name}' matched {count} times (expected 1) in {target}. "
                  "Update patches/patch_voice_barge.py.", file=sys.stderr)
            return 1
    out = src
    for _, anchor, repl in edits:
        out = out.replace(anchor, repl, 1)
    compile(out, str(target), "exec")
    _write(target, out, crlf)
    print(f"  patched (voice barge): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: patch_voice_barge.py <file>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
