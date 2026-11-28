#!/usr/bin/env python3
"""The pill's Done in one exchange with the voice server (Talos on FELIX #2104).

The pill used to ask "status" and then send skip_forward: two socket exchanges. A recording
ending between them left a sticky skip_forward that cut the following reply. Now:

  control_channel.py: ControlState.request_done() requests skip_forward only while a recording
    runs and nothing speaks, decided under the lock set_listening takes. Converse reads and
    clears skip_forward after the recording returns, so a Done accepted here is consumed by
    the turn it was meant for.
  control_socket.py: {"command": "vml_done"} calls it and answers
    {"ok": true, "action": "vml_done", "request_id": ..., "applied": true|false}.

A voice server without this patch does not answer vml_done; the pill then falls back to the
old two-step check (voice_pill_state.request_done returns None).

Needs patch_voice_pill (is_listening) and patch_voice_barge (_speaking) first. Idempotent
(marker). Exits 1 naming the anchor when voice-mode no longer matches. Verified against
voice-mode 8.12.0 with the steps before it in manifest.json and manifest.windows.json.

Usage: patch_pill_done.py <voice_mode/control_channel.py> [<voice_mode/control_socket.py>]
"""
import sys
from pathlib import Path

MARKER = "voicemode-local pill done"

LISTENING = (
    "    def is_listening(self) -> bool:\n"
    "        with self._cond:\n"
    "            return self._listening\n"
)

CHANNEL = [
    ("request_done", LISTENING,
     LISTENING +
     "\n"
     f"    def request_done(self) -> bool:  # {MARKER}\n"
     '        """The pill\'s Done: skip_forward only while a recording runs and nothing speaks,\n'
     "        decided under the lock set_listening takes, so a recording that is ending cannot\n"
     '        leave a sticky skip_forward for the next reply (Talos on FELIX #2104)."""\n'
     "        with self._cond:  # a Condition on an RLock: request_skip_forward may take it again\n"
     "            if not self._listening or self._speaking:\n"
     "                return False\n"
     "            return self.request_skip_forward()\n", 1),
]

STATUS_CHECK = '        if not isinstance(payload, dict) or payload.get("command") != COMMAND_STATUS:\n'

SOCKET = [
    ("vml_done command", STATUS_CHECK,
     f'        if isinstance(payload, dict) and payload.get("command") == "vml_done":  # {MARKER}\n'
     "            # The pill's Done in one exchange: checked and applied under the state's lock.\n"
     "            applied = bool(self._state.request_done())\n"
     '            reply = {"ok": True, "action": "vml_done", "request_id": payload.get("request_id"),\n'
     '                     "applied": applied}\n'
     "            try:\n"
     '                conn.sendall((json.dumps(reply) + "\\n").encode("utf-8"))\n'
     "            except OSError:\n"
     "                pass\n"
     "            return True\n" + STATUS_CHECK, 1),
]


def apply(target: Path) -> int:
    if target.name == "control_channel.py":
        return _apply(target, CHANNEL)
    if target.name == "control_socket.py":
        return _apply(target, SOCKET)
    print(f"{target}: not a file this patch knows", file=sys.stderr)
    return 1


def _apply(target: Path, edits) -> int:
    source = target.read_bytes().decode("utf-8")
    if MARKER in source:
        return 0
    ending = "\r\n" if "\r\n" in source else "\n"
    text = source.replace("\r\n", "\n")
    for name, old, new, count in edits:
        found = text.count(old)
        if found != count:
            print(f"{target}: anchor '{name}' found {found} times, expected {count}; "
                  "voice-mode or an earlier patch has changed", file=sys.stderr)
            return 1
        text = text.replace(old, new)
    compile(text, str(target), "exec")
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))
    return 0


if __name__ == "__main__":
    rc = 0
    for arg in sys.argv[1:]:
        rc = apply(Path(arg)) or rc
    sys.exit(rc)
