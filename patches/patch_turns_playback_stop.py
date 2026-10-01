#!/usr/bin/env python3
"""Stop the turns[] playback thread when converse is cancelled.

Downstream carry of upstream PR #555 (issue #554). Delete this patch once that
merges and the pin moves past it.

The defect
==========
``_speak_turns_pipeline`` plays each turn with
``await asyncio.to_thread(_play_samples_blocking, ...)``. Cancelling that await
(ESC, or the transport-close cancellation VM-2015 restored) abandons the future
but not the worker thread, so the turn plays to the end. On shutdown it also
blocks exit: ``mcp.run()`` ends in ``asyncio.Runner.close()``, which joins the
default executor for up to 300s, so ``mcp.run()`` itself cannot return while
the turn is still playing. The client has meanwhile started a replacement
server; on WSLg the two output streams mix on the shared RDP sink (stutter plus
stale trailing audio).

Measured for #555 (WSL2/WSLg, real stdio server, real pipeline and player, one
silent 25s turn, stdin closed 5s in): process exit 19.8-20.1s after stdin close
on 8.12.0 (n=3), 0.37-0.46s with this change (n=3).

This replaces patch_shutdown_abort.py, which force-exited *after* mcp.run()
returned. The wait is inside mcp.run(), so that patch could not reach it
(12.6s vs 13.0s linger with and without it, n=1 each).

The fix
=======
Same shape as VM-2015's recording stop flag: the caller passes a
``threading.Event`` into the worker and sets it on ``CancelledError``; the
worker starts the player non-blocking, polls every 50ms and stops the player
when the event is set. Natural completion still ends in ``player.wait()``.

Anchors verified against voice-mode 8.12.0 (2026-10-01).
Idempotent; fails loudly on drift.

Usage: patch_turns_playback_stop.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local turns playback stop"

A_HELPER = (
    "def _play_samples_blocking(samples, sample_rate):\n"
    '    """Play decoded samples to completion (blocking). Runs in a worker thread\n'
    '    via ``asyncio.to_thread`` so the producer keeps synthesizing during playback."""\n'
    "    player = NonBlockingAudioPlayer()\n"
    "    player.play(samples, sample_rate, blocking=True)\n"
)
R_HELPER = (
    "def _play_samples_blocking(samples, sample_rate, stop_event: Optional[threading.Event] = None):\n"
    '    """Play decoded samples to completion (blocking). Runs in a worker thread\n'
    "    via ``asyncio.to_thread`` so the producer keeps synthesizing during playback.\n"
    "\n"
    "    voicemode-local turns playback stop (upstream PR #555): ``stop_event`` is\n"
    "    the caller's side channel into this thread, as with the recording stop\n"
    "    flag (VM-2015). Cancelling the awaiting coroutine abandons the future but\n"
    "    not the thread, so the caller sets the event and we stop the player at the\n"
    "    next poll instead of playing out the rest of the turn.\n"
    '    """\n'
    "    if stop_event is not None and stop_event.is_set():\n"
    "        return\n"
    "    player = NonBlockingAudioPlayer()\n"
    "    player.play(samples, sample_rate, blocking=False)\n"
    "    while not player.playback_complete.wait(timeout=0.05):\n"
    "        if stop_event is not None and stop_event.is_set():\n"
    "            player.stop()\n"
    "            return\n"
    "    player.wait()\n"
)

A_CALL = (
    "                play_start = time.perf_counter()\n"
    "                try:\n"
    '                    await asyncio.to_thread(_play_samples_blocking, item["samples"], item["sample_rate"])\n'
    "                except Exception as e:\n"
)
R_CALL = (
    "                play_start = time.perf_counter()\n"
    "                # Cancelling this await abandons the future, not the thread.\n"
    "                # Left running, the turn plays to the end after ESC, and on\n"
    "                # shutdown asyncio.Runner.close() joins the default executor,\n"
    "                # so mcp.run() cannot return and the process cannot exit.\n"
    "                stop_playback = threading.Event()\n"
    "                try:\n"
    "                    await asyncio.to_thread(\n"
    '                        _play_samples_blocking, item["samples"], item["sample_rate"],\n'
    "                        stop_event=stop_playback,\n"
    "                    )\n"
    "                except asyncio.CancelledError:\n"
    "                    stop_playback.set()\n"
    "                    raise\n"
    "                except Exception as e:\n"
)


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0

    for name, anchor in (("_play_samples_blocking", A_HELPER), ("turns playback call", A_CALL)):
        count = src.count(anchor)
        if count != 1:
            print(
                f"ANCHOR DRIFT: '{name}' matched {count} times (expected 1) in "
                f"{target}. Upstream converse.py changed (has PR #555 merged?) — "
                f"update or retire patches/patch_turns_playback_stop.py.",
                file=sys.stderr,
            )
            return 1

    out = src.replace(A_HELPER, R_HELPER, 1).replace(A_CALL, R_CALL, 1)
    compile(out, str(target), "exec")  # syntax safety net before writing
    target.write_text(out)
    print(f"  patched (turns playback stop): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
