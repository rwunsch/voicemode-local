"""Subprocess helper for voice_queue contention tests.

Usage:
    queue_helper.py claim <base>                  try one claim, print WON/LOST
    queue_helper.py hold <base> <secs> <beat>     claim, heartbeat every <beat>
                                                  for <secs>, then release
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "patches"))
import voice_queue  # noqa: E402


def main():
    cmd, base = sys.argv[1], Path(sys.argv[2])
    if cmd == "claim":
        won = voice_queue.try_claim_floor(base, "racer", "v")
        print("WON" if won else "LOST", flush=True)
        if won:
            time.sleep(30)  # stay alive so the floor stays live; test kills us
    elif cmd == "hold":
        secs, beat = float(sys.argv[3]), float(sys.argv[4])
        if not voice_queue.try_claim_floor(base, "holder", "v"):
            print("FAILED_TO_CLAIM")
            return
        end = time.monotonic() + secs
        while time.monotonic() < end:
            voice_queue.heartbeat_floor(base)
            time.sleep(beat)
        voice_queue.release_floor(base)


if __name__ == "__main__":
    main()
