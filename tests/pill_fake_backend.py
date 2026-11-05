"""Test-only stand-in for patches/voice_pill.py, for driving pill-windows.ps1 by hand.

Usage: pill_fake_backend.py <dir>

Each second it emits <dir>/state.json merged over a default snapshot, and appends
every line received on stdin to <dir>/commands.log. Voice, language, barge and beeps commands
take effect in what it emits, so those controls hold when clicked. <dir>/startup_exit, if present,
holds an exit code to use at once, before printing anything (a refused second instance
exits 1 this way). <dir>/exit_now does the same after output has started, and is deleted
first. It touches no voice-mode state, opens no mic and plays no audio.
"""
import json
import os
import select
import sys
import time
from pathlib import Path

DEFAULT = {"visible": True, "phase": "listening", "floor": {"pid": 1, "label": "fake"},
           "sessions": [{"pid": 1, "label": "fake", "state": "talking", "position": None},
                        {"pid": 2, "label": "other", "state": "waiting", "position": 1}],
           "voice": "af_sky", "language": "English",
           "barge": {"enabled": False, "available": True, "count": 2}, "beeps": False,
           "platform": "windows", "ui": {}}


def apply_command(line: bytes, live: dict) -> None:
    try:
        msg = json.loads(line)
    except ValueError:
        return
    if not isinstance(msg, dict):
        return
    cmd = msg.get("cmd")
    if cmd == "set_voice":
        live["voice"] = msg.get("voice")
    elif cmd == "set_language":
        live["language"] = msg.get("language")
    elif cmd == "set_beeps":
        live["beeps"] = bool(msg.get("on"))
    elif cmd == "set_barge":
        live["barge"] = {**DEFAULT["barge"], "enabled": bool(msg.get("on"))}


def main(folder: str) -> int:
    d = Path(folder)
    d.mkdir(parents=True, exist_ok=True)
    gate = d / "startup_exit"
    if gate.exists():
        print("fake backend: refusing to start", file=sys.stderr)
        return int(gate.read_text().strip() or 1)
    with (d / "runs.log").open("a") as f:
        f.write(f"{time.time():.1f} pid={os.getpid()}\n")
    buf = b""
    live = {}
    while True:
        try:
            state = json.loads((d / "state.json").read_text())
        except (OSError, ValueError):
            state = {}
        print(json.dumps({**DEFAULT, **state, **live}), flush=True)
        stop = d / "exit_now"
        if stop.exists():
            code = int(stop.read_text().strip() or 1)
            stop.unlink()
            return code
        ready, _, _ = select.select([0], [], [], 1.0)
        if ready:
            chunk = os.read(0, 65536)
            if not chunk:
                with (d / "runs.log").open("a") as f:
                    f.write(f"{time.time():.1f} stdin EOF\n")
                return 0
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                with (d / "commands.log").open("ab") as f:
                    f.write(line + b"\n")
                apply_command(line, live)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
