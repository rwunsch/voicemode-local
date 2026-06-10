#!/usr/bin/env python3
"""Patch voice_mode/simple_failover.py to remove the silent OpenAI voice swap.

Upstream simple_tts_failover maps a local voice (e.g. af_sky) to an OpenAI voice
(nova) when it falls through to the OpenAI endpoint, so a Kokoro/Piper outage
silently switches the user to a cloud voice mid-conversation. voicemode-local
policy is "OpenAI last-resort, no silent swaps": pass the requested voice through
unchanged — if OpenAI doesn't own it the request fails loudly instead of quietly
becoming a surprise cloud voice.

The OpenAI voice-mapping block is replaced between two short, stable marker
lines (robust to the surrounding restructuring upstream does). Idempotent;
fails loudly if the markers drift. Verified against voice-mode 8.7.1.

Usage: patch_simple_failover.py [<path-to-simple_failover.py>]
"""
import sys
from pathlib import Path

MARKER = "NO SILENT SWAP (voicemode-local)"

# Start: the OpenAI voice-mapping intro comment. End: the Kokoro else-branch we
# keep. Everything between (the openai_voices list + if/else voice_mapping) is
# replaced with a straight pass-through.
B_START = "            # Map Kokoro voices to OpenAI equivalents, or use OpenAI default\n"
B_END = "        else:\n            selected_voice = voice  # Use original voice for Kokoro"
B_REPLACE = (
    "            # NO SILENT SWAP (voicemode-local): never substitute a local\n"
    "            # voice with an OpenAI one. Pass the requested voice through\n"
    "            # unchanged; if OpenAI does not own it the request fails loudly\n"
    "            # rather than quietly becoming a surprise cloud voice. Request an\n"
    "            # OpenAI voice (alloy/echo/fable/nova/onyx/shimmer/...) to use it.\n"
    "            selected_voice = voice\n"
)


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return 0
    for name, marker in (("start", B_START), ("end", B_END)):
        if src.count(marker) != 1:
            print(f"ANCHOR DRIFT: marker '{name}' matched {src.count(marker)} "
                  f"times (expected 1) in {target}. Upstream simple_failover.py "
                  f"changed — update patch_simple_failover.py.", file=sys.stderr)
            return 1
    si, ei = src.index(B_START), src.index(B_END)
    if not si < ei:
        print("ANCHOR DRIFT: start marker not before end marker.", file=sys.stderr)
        return 1
    out = src[:si] + B_REPLACE + src[ei:]
    compile(out, str(target), "exec")
    target.write_text(out)
    print(f"  patched (no silent swap): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "simple_failover.py"
    sys.exit(apply(target))
