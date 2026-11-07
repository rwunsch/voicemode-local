#!/usr/bin/env python3
"""Push-to-talk acts the moment the key goes down, and a tap stops cleanly.

Why this exists
===============
Robert's rule (2026-11-07, every platform): key down opens the mic at once and
cuts the reply; a release within 0.8 s is a full stop, back to text mode. The
listeners now send ``hold_start`` on key-down and ``stop`` for a tap
(ptt_core.py, ptt_control_client.py). Three gaps remained on the server:

1. **Generation was deaf.** ``hold_start`` cut playback (patch_hold_barges_in)
   but nothing polled the control state while the TTS server was still
   producing the reply: Kokoro's time to first audio was 7.9 s on the Windows
   box, and a press in that window waited for it. ``_ptt_cuttable_tts`` runs the
   TTS call as a task and polls every 50 ms; on a hold or a stop it cancels the
   call and reports a cut (success, nothing played), so converse goes straight
   on to listening, or to the stop result. While audio is already playing
   (``is_speaking``, from patch_voice_barge) it first gives the player 0.3 s to
   cut itself, because cancelling the buffered player's wait would leave the
   device running.
2. **The recording started late.** The listen path slept 0.5 s and played the
   "listening" chime before recording. With the key held the user is already
   talking, so both are skipped (as voice barge-in does).
3. **A stop left the hold latched.** ``request_stop`` now clears it, so the tap
   (hold_start then stop) leaves no hold behind; the recording loop already
   breaks on ``is_stopped`` and listen_and_transcribe returns "stopped" before
   speech-to-text, so the partial recording is dropped untranscribed.

Plus the pill: the status reply carries ``holding`` so it can show "Holding...".

Order: after patch_control_hold (``_holding``), patch_voice_barge (``is_speaking``)
and patch_voice_pill (the status line it anchors on). Targets control_channel.py,
control_socket.py and tools/converse.py.

Anchors verified against voice-mode 8.12.0 with the manifest's earlier patches
(2026-11-07). Idempotent; CRLF-safe; fails loudly on drift.

Usage: patch_ptt_instant.py <control_channel.py|control_socket.py|converse.py>
"""
import sys
from pathlib import Path

MARKER = "voicemode-local ptt instant"

# --- control_channel.py: a stop clears the hold ------------------------------
A_STOP = (
    "        with self._cond:\n"
    "            if self._state != STATE_STOPPED:\n"
    "                self._state = STATE_STOPPED\n"
)
R_STOP = (
    "        with self._cond:\n"
    "            # voicemode-local ptt instant: a push-to-talk tap is hold_start then\n"
    "            # stop. The stop ends the turn and must not leave the hold latched.\n"
    "            self._holding = False\n"
    "            if self._state != STATE_STOPPED:\n"
    "                self._state = STATE_STOPPED\n"
)

# --- control_socket.py: the pill can see the hold ----------------------------
A_STATUS = '        "listening": control_state.is_listening,  # voicemode-local voice pill\n'
R_STATUS = (
    '        "listening": control_state.is_listening,  # voicemode-local voice pill\n'
    f'        "holding": getattr(snap, "is_holding", False),  # {MARKER}\n'
)

# --- converse.py: the helper -------------------------------------------------
A_HELPER = "\n\nasync def text_to_speech_with_failover(\n"
R_HELPER = (
    "\n\n"
    f"# {MARKER}: a push-to-talk press (hold_start) or a stop cuts the reply even\n"
    "# while the TTS server is still generating it -- nothing else polls the control\n"
    "# state before the first sample plays. See patches/patch_ptt_instant.py.\n"
    "_PTT_CUT_POLL_S = 0.05\n"
    "_PTT_CUT_GRACE_S = 0.3\n"
    "\n"
    "\n"
    "async def _ptt_cuttable_tts(control_state, tts, **kwargs):\n"
    "    \"\"\"``await tts(**kwargs)``, cut short by a PTT hold or a stop.\n"
    "\n"
    "    Returns tts's own result, or after a cut ``(True, metrics, config)`` with\n"
    "    ``metrics['ptt_cut']`` set and nothing played, so the caller moves on to the\n"
    "    hold's recording or the stop's result exactly as after a cut playback.\n"
    "    \"\"\"\n"
    "    task = asyncio.ensure_future(tts(**kwargs))\n"
    "    started = time.perf_counter()\n"
    "    try:\n"
    "        while True:\n"
    "            done, _ = await asyncio.wait({task}, timeout=_PTT_CUT_POLL_S)\n"
    "            if done:\n"
    "                return task.result()\n"
    "            snap = control_state.snapshot()\n"
    "            if not (snap.is_stopped or getattr(snap, \"is_holding\", False)):\n"
    "                continue\n"
    "            if getattr(control_state, \"is_speaking\", False):\n"
    "                # Playing: the playback loop stops the device on this same flag.\n"
    "                # Cancelling its wait instead could leave the player running.\n"
    "                done, _ = await asyncio.wait({task}, timeout=_PTT_CUT_GRACE_S)\n"
    "                if done:\n"
    "                    return task.result()\n"
    "            task.cancel()\n"
    "            await asyncio.wait({task})\n"
    "            if not task.cancelled():\n"
    "                if task.exception() is None:\n"
    "                    return task.result()  # it finished before the cancel landed\n"
    "                logger.debug(\"TTS failed while being cut\", exc_info=task.exception())\n"
    "            elapsed = time.perf_counter() - started\n"
    "            logger.info(\"TTS cut by push-to-talk after %.1fs, before playback\", elapsed)\n"
    "            return True, {\"ttfa\": 0.0, \"generation\": elapsed, \"playback\": 0.0,\n"
    "                          \"total\": elapsed, \"ptt_cut\": True}, \\\n"
    "                {\"provider\": \"ptt-cut\", \"voice\": kwargs.get(\"voice\")}\n"
    "    except asyncio.CancelledError:\n"
    "        task.cancel()\n"
    "        raise\n"
    "\n"
    "\n"
    "async def text_to_speech_with_failover(\n"
)

# --- converse.py: the main speak call goes through it -----------------------
A_CALL = (
    "                        tts_success, tts_metrics, tts_config = await text_to_speech_with_failover(\n"
    "                            message=message,\n"
)
R_CALL = (
    f"                        # {MARKER}: a press cuts generation too.\n"
    "                        tts_success, tts_metrics, tts_config = await _ptt_cuttable_tts(control_state, text_to_speech_with_failover,\n"
    "                            message=message,\n"
)

# --- converse.py: record at once while the key is held -----------------------
A_LISTEN = (
    "    if pre_listen_pause:\n"
    "        await asyncio.sleep(pre_listen_pause)\n"
)
R_LISTEN = (
    f"    # {MARKER}: a held push-to-talk key means they are talking already (the\n"
    "    # press cut the reply). Record now: no pause, no chime.\n"
    "    if getattr(control_state.snapshot(), \"is_holding\", False):\n"
    "        pre_listen_pause = 0.0\n"
    "        chime_enabled = False\n"
    "\n"
    "    if pre_listen_pause:\n"
    "        await asyncio.sleep(pre_listen_pause)\n"
)

TARGETS = {
    "control_channel.py": [("stop clears the hold", A_STOP, R_STOP, 1)],
    "control_socket.py": [("status holding", A_STATUS, R_STATUS, 1)],
    "converse.py": [("cuttable tts helper", A_HELPER, R_HELPER, 1),
                    ("speak call", A_CALL, R_CALL, 1),
                    ("record at once while held", A_LISTEN, R_LISTEN, 1)],
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
        print(f"[patch_ptt_instant] ERROR: unexpected target {target.name}", file=sys.stderr)
        return 1
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, anchor, _, expected in edits:
        count = src.count(anchor)
        if count != expected:
            print(
                f"ANCHOR DRIFT: '{name}' matched {count} times (expected {expected}) in "
                f"{target}. Update patches/patch_ptt_instant.py (it runs after the hold, "
                f"barge and pill patches).",
                file=sys.stderr,
            )
            return 1
    out = src
    for _, anchor, repl, expected in edits:
        out = out.replace(anchor, repl, expected)
    compile(out, str(target), "exec")  # syntax safety net before writing
    _write(target, out, crlf)
    print(f"  patched (ptt instant): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: patch_ptt_instant.py <control_channel.py|control_socket.py|converse.py>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
