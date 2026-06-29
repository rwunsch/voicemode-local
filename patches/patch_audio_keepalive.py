#!/usr/bin/env python3
"""Patch voice_mode/core.py to keep the queue floor alive during audio playback.

Root cause of the simultaneous-speech bug
==========================================
`NonBlockingAudioPlayer.play()` drives audio on a PortAudio callback thread
(real OS thread). The asyncio event loop's `player.wait()` call blocks in the
executor waiting for `player.playback_complete` — but when WSLg's RDPSink is
CPU-starved, the PortAudio write that starts the stream can block the calling
OS thread in D-state (uninterruptible sleep). That starves the asyncio event
loop, so the heartbeat task (`QueueSession.start_heartbeat()` in
patches/voice_queue.py) can't fire, `floor.last_activity` goes stale, and a
waiting session sees the floor as "wedged" and reclaims it.

Now two holders exist: the reclaimer starts speaking, AND the original holder's
D-state unblocks, its buffered audio drains, and both voices overlap.

The fix: a plain `threading.Thread` (not asyncio) polls `player.playback_complete`
and calls `heartbeat_floor()` while audio is genuinely playing. A D-state block
on the audio thread cannot touch the keepalive thread — the OS schedules them
independently. The floor becomes impossible to steal during active playback.

Anchor: the NonBlockingAudioPlayer call site in core.py's text_to_speech_stream
function. The replacement wraps the play/wait pair in a try/finally that launches
and shuts down the keepalive thread safely. threading is imported inline to avoid
touching the module-level import block.

Idempotent; fails loudly on anchor drift. Verified against voice-mode 8.7.1.

Usage: patch_audio_keepalive.py [<path-to-core.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local audio keepalive"

ANCHOR = (
    "                        # Use non-blocking audio player for concurrent playback support\n"
    "                        player = NonBlockingAudioPlayer()\n"
    "                        player.play(samples_with_buffer, audio.frame_rate, blocking=False)\n"
    "                        player.wait()\n"
)

REPLACE = (
    "                        # Use non-blocking audio player for concurrent playback support\n"
    "                        player = NonBlockingAudioPlayer()\n"
    "                        # voicemode-local audio keepalive: a real OS thread (not asyncio)\n"
    "                        # refreshes the queue floor while audio plays, so a D-state\n"
    "                        # block on the WSLg RDPSink can't let last_activity go stale\n"
    "                        # and cause a premature floor reclaim / simultaneous-speech\n"
    "                        # overlap. See patches/patch_audio_keepalive.py for the full why.\n"
    "                        import threading as _vml_threading\n"
    "                        _vml_stop = _vml_threading.Event()\n"
    "                        def _vml_keepalive(_stop, _player):\n"
    "                            try:\n"
    "                                import voice_queue as _vq\n"
    "                            except ImportError:\n"
    "                                return\n"
    "                            _base = _vq.DEFAULT_BASE\n"
    "                            _interval = _vq.HEARTBEAT_INTERVAL\n"
    "                            while not _stop.wait(timeout=_interval):\n"
    "                                if _player.playback_complete.is_set():\n"
    "                                    break\n"
    "                                _vq.heartbeat_floor(_base)\n"
    "                        _vml_thread = _vml_threading.Thread(\n"
    "                            target=_vml_keepalive,\n"
    "                            args=(_vml_stop, player),\n"
    "                            daemon=True,\n"
    "                            name='vm-audio-keepalive',\n"
    "                        )\n"
    "                        _vml_thread.start()\n"
    "                        try:\n"
    "                            player.play(samples_with_buffer, audio.frame_rate, blocking=False)\n"
    "                            player.wait()\n"
    "                        finally:\n"
    "                            _vml_stop.set()\n"
)


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    count = src.count(ANCHOR)
    if count != 1:
        print(
            f"ANCHOR DRIFT: expected 1 match, got {count} in {target}. "
            f"Upstream core.py changed — update patch_audio_keepalive.py.",
            file=sys.stderr,
        )
        return 1
    out = src.replace(ANCHOR, REPLACE, 1)
    compile(out, str(target), "exec")
    target.write_text(out)
    print(f"  patched (audio keepalive): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "core.py"
    sys.exit(apply(target))
