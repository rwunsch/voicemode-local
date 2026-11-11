#!/usr/bin/env python3
"""Fair holds, an MCP progress heartbeat, and a logged cancelled wait in tools/converse.py.

1. At the end of a turn, hold_conch goes through conch_hold.effective_hold: while someone
   else waits, a held turn without a reply releases, and at most 3 held turns in a row
   (VOICEMODE_CONCH_HOLD_MAX_TURNS) keep the mic.
2. The converse tool runs inside mcp_progress.heartbeat, so a client with a 180 s tool
   timeout (Copilot CLI) sees progress every 30 s while converse waits or listens.
3. A wait in the conch queue that is cancelled logs CONCH_WAIT_END result=cancelled;
   upstream deregistered it silently.

Idempotent (marker). Stops setup if an anchor is not found exactly once. Verified against
pristine voice-mode 8.12.0 and the fully patched tree. Every new branch fails safe to
upstream behaviour.
"""
import sys
from pathlib import Path

MARKER = "VML: CONCH FAIRNESS"

PAIRS = [
    ("            held_seconds = conch.release(hold=hold_conch)\n",
     "            try:  # " + MARKER + ": a hold does not starve a waiting session\n"
     "                from voice_mode import conch_hold as _vml_ch\n"
     "                _vml_speak_only = \"turn_results\" in locals() or (not turns and not wait_for_response)\n"
     "                _vml_replied = _vml_speak_only or _vml_ch.is_reply(locals().get(\"response_text\")) \\\n"
     "                    or bool(locals().get(\"n_answered\"))\n"
     "                _vml_at = conch._acquire_time.timestamp() if conch._acquire_time else None\n"
     "                _vml_hold = _vml_ch.effective_hold(hold_conch, conch.session_id, replied=_vml_replied,\n"
     "                                                   acquired_at=_vml_at,\n"
     "                                                   expiry_s=float(conch.hold_timeout or 10.0))\n"
     "            except Exception:\n"
     "                _vml_hold = hold_conch\n"
     "            held_seconds = conch.release(hold=_vml_hold)\n"),
    ("                    \"hold_active\": bool(hold_conch)\n",
     "                    \"hold_active\": bool(_vml_hold)  # " + MARKER + ": the hold applied\n"),
    ("                while not conch.try_acquire() and waited < conch_wait_timeout:\n"
     "                    await asyncio.sleep(CONCH_CHECK_INTERVAL)\n"
     "                    waited += CONCH_CHECK_INTERVAL\n",
     "                try:  # " + MARKER + ": a cancelled wait is logged\n"
     "                    while not conch.try_acquire() and waited < conch_wait_timeout:\n"
     "                        await asyncio.sleep(CONCH_CHECK_INTERVAL)\n"
     "                        waited += CONCH_CHECK_INTERVAL\n"
     "                except asyncio.CancelledError:\n"
     "                    if event_logger:\n"
     "                        event_logger.log_event(\"CONCH_WAIT_END\", {\n"
     "                            \"pid\": os.getpid(),\n"
     "                            \"waited_seconds\": waited,\n"
     "                            \"result\": \"cancelled\"\n"
     "                        })\n"
     "                    raise\n"),
    ("                holder_agent = holder.get('agent', 'unknown') if holder else 'unknown'\n",
     "                holder_agent = holder.get('agent', 'unknown') if holder else 'unknown'\n"
     "                if holder is None:  # " + MARKER + ": a free mic reserved for a queued turn\n"
     "                    try:\n"
     "                        from voice_mode import conch_wakers as _vml_cw\n"
     "                        holder_agent = _vml_cw.reserved_label() or holder_agent\n"
     "                    except Exception:\n"
     "                        pass\n"),
    ("    result = await _converse_core(\n",
     "    try:  # " + MARKER + ": progress every 30 s so a 180 s client timeout does not cancel us\n"
     "        from voice_mode.mcp_progress import heartbeat as _vml_heartbeat\n"
     "    except Exception:\n"
     "        from contextlib import nullcontext as _vml_heartbeat\n"
     "    async with _vml_heartbeat():\n"
     "        result = await _converse_core(\n"),
]


def _read(target):
    raw = target.read_bytes()
    ending = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), ending


def _write(target, text, ending):
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))


def apply(target):
    source, ending = _read(target)
    if MARKER in source:
        return 0
    if target.name != "converse.py":
        print(f"[patch_conch_fairness] {target}: only tools/converse.py is patched", file=sys.stderr)
        return 1
    for anchor, _ in PAIRS:
        if source.count(anchor) != 1:
            print(f"[patch_conch_fairness] {target}: an anchor was found {source.count(anchor)} times, not once",
                  file=sys.stderr)
            return 1
    for anchor, replacement in PAIRS:
        source = source.replace(anchor, replacement)
    compile(source, str(target), "exec")
    _write(target, source, ending)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_conch_fairness.py <path to tools/converse.py>", file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
