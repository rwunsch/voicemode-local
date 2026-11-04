#!/usr/bin/env python3
"""Give voice-mode what the voice pill needs to show true state and act on it.

1. ``listening`` in the status reply, true while a recording call is running.
   The control socket exists only during a voice turn, so no socket means the
   agent is working.

2. ``speaking`` on the chunked streaming path, so the Mac can show "Speaking" and
   barge-in is not inert there. patch_voice_barge covers the other two players.
   No wrapped player calls another, so none can turn ``speaking`` off early.

3. Session registration, Stop and machine-wide Pause. ``converse`` registers and
   waits before the conch is taken, so a paused or stopped session never holds
   the floor. A session already queued for the conch when Pause is pressed is
   held again as its audio operation starts, and a listen waits before the mic
   opens.

4. Pause during recording: the loop ends, the audio is discarded, and the listen
   restarts after resume. A flag rather than a second is_paused() read, so a
   pause released at once cannot let the partial audio through. The fixed-duration
   recorder (``record_audio``) blocked in ``sd.wait()``, so it polls instead and
   does the same.

5. The pill's voice override replaces the caller's voice. Its language override is
   added to the result for the agent, not spoken.

The logic lives in voice_pill_state.py, which needs no server imports. A failure
there must not cost a turn, so the converse helpers catch and log.

Apply after patch_voice_barge, whose output it anchors on. Anchors verified against
voice-mode 8.12.0 (2026-11-04). Idempotent; fails loudly on drift.

Usage: patch_voice_pill.py <control_channel.py|control_socket.py|converse.py|streaming.py>
"""
import sys
from pathlib import Path

MARKER = "voicemode-local voice pill"

CHANNEL = [
    ("listening field",
     "        self._speaking: bool = False  # voicemode-local voice barge\n",
     "        self._speaking: bool = False  # voicemode-local voice barge\n"
     f"        self._listening: bool = False  # {MARKER}\n"),
    ("listening methods",
     "    def request_barge_in(self) -> bool:\n",
     f"    # {MARKER}: whether a recording call is running.\n"
     "    def set_listening(self, listening: bool) -> None:\n"
     "        with self._cond:\n"
     "            self._listening = listening\n"
     "\n"
     "    @property\n"
     "    def is_listening(self) -> bool:\n"
     "        with self._cond:\n"
     "            return self._listening\n"
     "\n"
     "    def request_barge_in(self) -> bool:\n"),
]

SOCKET = [
    ("status field",
     '        "speaking": control_state.is_speaking,  # voicemode-local voice barge\n',
     '        "speaking": control_state.is_speaking,  # voicemode-local voice barge\n'
     f'        "listening": control_state.is_listening,  # {MARKER}\n'),
]

UNTIL_STOPPED = "until=lambda: control_state.snapshot().is_stopped"
RECORD_ARGS = "max_duration, disable_silence_detection, min_duration, vad_aggressiveness, stop_event"
RECORD_SIG = (
    "(max_duration: float, disable_silence_detection: bool = False, min_duration: float = 0.0, "
    "vad_aggressiveness: Optional[int] = None, stop_event: Optional[threading.Event] = None) "
    "-> Tuple[np.ndarray, bool]:\n"
)

CONVERSE = [
    ("import",
     "from voice_mode.control_socket import start_control_listener, stop_control_listener\n",
     "from voice_mode.control_socket import start_control_listener, stop_control_listener\n"
     f"from voice_mode import voice_pill_state  # {MARKER}\n"),
    ("fixed-duration recording",
     "            dtype=np.int16\n        )\n        sd.wait()\n",
     "            dtype=np.int16\n        )\n"
     f"        # {MARKER}: poll rather than block, so Pause closes this mic too.\n"
     "        while sd.get_stream().active:\n"
     "            if voice_pill_state.is_paused():\n"
     '                logger.info("Recording ended by Pause -- discarding it")\n'
     "                sd.stop()\n"
     "                _voice_pill_discard.set()\n"
     "                return np.array([])\n"
     "            time.sleep(0.05)\n"
     "        sd.wait()  # closes the finished stream\n"),
    ("helpers and recording wrapper",
     "def record_audio_with_silence_detection" + RECORD_SIG,
     f"# {MARKER}: set when Pause ended the recording, so its audio is discarded.\n"
     "_voice_pill_discard = threading.Event()\n"
     "\n"
     "\n"
     "async def _voice_pill_enter(voice: Optional[str]) -> Optional[str]:\n"
     '    """Register, wait out a Pause, and return a message if this session was stopped."""\n'
     "    try:\n"
     "        return await voice_pill_state.enter(\n"
     "            os.getpid(), _vml_session_name(), voice or (TTS_VOICES[0] if TTS_VOICES else None))\n"
     "    except Exception:\n"
     '        logger.warning("voice pill: could not enter the turn", exc_info=True)\n'
     "        return None\n"
     "\n"
     "\n"
     "def _voice_pill_voice(voice: Optional[str]) -> Optional[str]:\n"
     "    try:\n"
     "        return voice_pill_state.effective_voice(os.getpid(), voice)\n"
     "    except Exception:\n"
     '        logger.warning("voice pill: could not read the override", exc_info=True)\n'
     "        return voice\n"
     "\n"
     "\n"
     "def _voice_pill_result(result: str) -> str:\n"
     "    try:\n"
     "        return result + voice_pill_state.language_note(os.getpid())\n"
     "    except Exception:\n"
     '        logger.warning("voice pill: could not read the override", exc_info=True)\n'
     "        return result\n"
     "\n"
     "\n"
     f"# {MARKER}: report a running recording for the length of the call.\n"
     "def record_audio_with_silence_detection" + RECORD_SIG +
     "    control_state = get_control_state()\n"
     "    _voice_pill_discard.clear()\n"
     "    control_state.set_listening(True)\n"
     "    try:\n"
     f"        return record_audio_with_silence_detection_inner({RECORD_ARGS})\n"
     "    finally:\n"
     "        control_state.set_listening(False)\n"
     "\n"
     "\n"
     "def record_audio_with_silence_detection_inner" + RECORD_SIG),
    ("recording loop",
     '                        logger.info("🛑 Recording stopped via control channel")\n'
     "                        stop_recording = True\n"
     "                        break\n",
     '                        logger.info("🛑 Recording stopped via control channel")\n'
     "                        stop_recording = True\n"
     "                        break\n"
     f"                    # {MARKER}: Pause closes the mic now. listen_and_transcribe\n"
     "                    # discards what was captured and listens again after resume.\n"
     "                    if voice_pill_state.is_paused():\n"
     '                        logger.info("Recording ended by Pause -- discarding it")\n'
     "                        _voice_pill_discard.set()\n"
     "                        break\n"),
    ("audio operation start",
     "    start_control_listener()\n    try:\n        yield control_state\n",
     "    start_control_listener()\n    try:\n"
     f"        # {MARKER}: this session may have queued for the conch before Pause.\n"
     f"        await voice_pill_state.wait_while_paused({UNTIL_STOPPED})\n"
     "        yield control_state\n"),
    ("listen start",
     "    timings: Dict[str, float] = {}\n\n    # voicemode-local voice barge:",
     "    timings: Dict[str, float] = {}\n\n"
     f"    # {MARKER}: Pause keeps the mic closed. A stop during the wait ends this.\n"
     f"    await voice_pill_state.wait_while_paused({UNTIL_STOPPED})\n"
     "    if control_state.snapshot().is_stopped:\n"
     '        return ListenResult(outcome="stopped", control=control_state.snapshot(), timings=timings)\n'
     "\n"
     "    # voicemode-local voice barge:"),
    ("discard and relisten",
     "    # VM-1676: a control-channel stop that arrived while we were listening ends\n",
     f"    # {MARKER}: nothing recorded under Pause reaches speech-to-text. The\n"
     "    # relisten waits for resume at its start. Barge state was consumed above.\n"
     "    if _voice_pill_discard.is_set() or voice_pill_state.is_paused():\n"
     "        _voice_pill_discard.clear()\n"
     "        if not control_state.snapshot().is_stopped:\n"
     "            return await listen_and_transcribe(\n"
     "                control_state,\n"
     "                listen_duration_max=listen_duration_max,\n"
     "                listen_duration_min=listen_duration_min,\n"
     "                disable_silence_detection=disable_silence_detection,\n"
     "                vad_aggressiveness=vad_aggressiveness,\n"
     "                chime_enabled=chime_enabled,\n"
     "                chime_leading_silence=chime_leading_silence,\n"
     "                chime_trailing_silence=chime_trailing_silence,\n"
     "                transport=transport,\n"
     "                event_logger=event_logger,\n"
     "                pre_listen_pause=pre_listen_pause,\n"
     "            )\n"
     "\n"
     "    # VM-1676: a control-channel stop that arrived while we were listening ends\n"),
    ("enter the turn",
     "    # Convert string booleans to actual booleans\n"
     "    if isinstance(wait_for_response, str):\n",
     f"    # {MARKER}: before the conch, so a waiting session does not hold the floor.\n"
     "    _voice_pill_stopped = await _voice_pill_enter(voice)\n"
     "    if _voice_pill_stopped:\n"
     "        return _voice_pill_stopped\n"
     "    voice = _voice_pill_voice(voice)\n"
     "\n"
     "    # Convert string booleans to actual booleans\n"
     "    if isinstance(wait_for_response, str):\n"),
    ("language note",
     "    return result + _build_widgets_segment(effective_time_in_response)\n",
     f"    # {MARKER}: the language override goes to the agent, not to speech.\n"
     "    return _voice_pill_result(result + _build_widgets_segment(effective_time_in_response))\n"),
]


def _stream_wrapper(name: str):
    return (f"{name}: report live playback",
            f"async def {name}(\n",
            f"# {MARKER}: report live playback for the length of the call.\n"
            f"async def {name}(*args, **kwargs) -> Tuple[bool, StreamMetrics]:\n"
            "    control_state = get_control_state()\n"
            "    control_state.set_speaking(True)\n"
            "    try:\n"
            f"        return await {name}_inner(*args, **kwargs)\n"
            "    finally:\n"
            "        control_state.set_speaking(False)\n"
            "\n"
            "\n"
            f"async def {name}_inner(\n")


STREAMING = [_stream_wrapper(n) for n in
             ("stream_pcm_audio", "stream_cartesia_pcm", "stream_with_buffering")]

TARGETS = {"control_channel.py": CHANNEL, "control_socket.py": SOCKET,
           "converse.py": CONVERSE, "streaming.py": STREAMING}


def apply(target: Path) -> int:
    edits = TARGETS.get(target.name)
    if edits is None:
        print(f"[patch_voice_pill] ERROR: unexpected target {target.name}", file=sys.stderr)
        return 1
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, anchor, _ in edits:
        count = src.count(anchor)
        if count != 1:
            print(f"ANCHOR DRIFT: '{name}' matched {count} times (expected 1) in {target}. "
                  "Update patches/patch_voice_pill.py.", file=sys.stderr)
            return 1
    out = src
    for _, anchor, repl in edits:
        out = out.replace(anchor, repl, 1)
    compile(out, str(target), "exec")
    target.write_text(out)
    print(f"  patched (voice pill): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: patch_voice_pill.py <file>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
