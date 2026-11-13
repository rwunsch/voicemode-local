#!/usr/bin/env python3
"""Patch voice_mode/simple_failover.py: three independent edits.

1. Remove the silent OpenAI voice swap.
2. Make the hardcoded 60s STT read timeout configurable.
3. Make the hardcoded 30s TTS timeout configurable.

Upstream simple_tts_failover maps a local voice (e.g. af_sky) to an OpenAI voice
(nova) when it falls through to the OpenAI endpoint, so a Kokoro/Piper outage
silently switches the user to a cloud voice mid-conversation. voicemode-local
policy is "OpenAI last-resort, no silent swaps": pass the requested voice through
unchanged — if OpenAI doesn't own it the request fails loudly instead of quietly
becoming a surprise cloud voice.

The OpenAI voice-mapping block is replaced between two short, stable marker
lines (robust to the surrounding restructuring upstream does). Idempotent;
fails loudly if the markers drift. Verified against voice-mode 8.7.1.

Edit 2 replaces `timeout=60.0` on the STT AsyncOpenAI client with a lookup of
two new env vars. Defaults are unchanged, so it is a no-op until an operator
sets one. See the injected comment for the measurements behind it. Verified
against voice-mode 8.12.0.

Edit 3 does the same for the TTS client's `timeout=30.0` (VOICEMODE_TTS_TIMEOUT,
VOICEMODE_TTS_TIMEOUT_LOCAL). Defaults unchanged. Verified against voice-mode 8.12.0.

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


MARKER_TIMEOUT = "STT TIMEOUT KNOB (voicemode-local)"

# Anchor: the helper goes immediately before the transient-error classifier,
# which sits just above simple_stt_failover and has been stable across releases.
T_ANCHOR = "def _is_transient_stt_error(e: Exception) -> bool:\n"

T_HELPER = '''# STT TIMEOUT KNOB (voicemode-local): upstream hardcodes a 60s read timeout for
# every STT endpoint. When a local transcription hangs, that 60s IS the cost of
# the stall -- the bounded VM-926 retry below then succeeds in about a second,
# so the user never sees an error, just a very reproducible ~61.5s silence.
#
# Measured on this machine 2026-09-06: 8 stalls in 36 converse calls (22%),
# every one of them 61.1-64.4s, which is 60.0 timeout + 0.5 backoff + the real
# transcription time -- matching to a tenth of a second. Zero such stalls in the
# 5,651 calls logged before that day.
#
# Making the timeout configurable lets a deployment trade stall length against
# its own worst-case transcription. Defaults are unchanged (60s everywhere), so
# this is a no-op unless the operator sets one of:
#   VOICEMODE_STT_TIMEOUT        seconds, all STT endpoints (default 60)
#   VOICEMODE_STT_TIMEOUT_LOCAL  seconds, local endpoints only (default: as above)
#
# Sizing guidance -- local STT time scales with audio length, so measure before
# lowering. GPU whisper (model `small`) here since 2026-08-25: n=67, median
# 1.1s, p95 3.6s, max 6.6s, so 15s is roughly 2x headroom. The SAME box on CPU
# whisper took 24-29s for a 10MB recording. Do not lower this on a CPU box.
def _stt_timeout(base_url: str) -> float:
    import os

    default = float(os.getenv("VOICEMODE_STT_TIMEOUT", "60.0"))
    if is_local_provider(base_url):
        return float(os.getenv("VOICEMODE_STT_TIMEOUT_LOCAL", str(default)))
    return default


'''

T_OLD = "                timeout=60.0,  # Allow time for slower transcriptions\n"
T_NEW = "                timeout=_stt_timeout(base_url),  # voicemode-local: see _stt_timeout\n"


def _read(target: Path):
    """The file as LF text, and whether it used CRLF. UTF-8 whatever the locale."""
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    """Write UTF-8 in the file's own line endings."""
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply_timeout_knob(target: Path) -> int:
    """Edit 2: make the STT client's 60s read timeout configurable."""
    src, crlf = _read(target)
    if MARKER_TIMEOUT in src:
        print(f"  already patched (stt timeout knob): {target}")
        return 0
    for name, marker in (("helper anchor", T_ANCHOR), ("timeout literal", T_OLD)):
        if src.count(marker) != 1:
            print(f"ANCHOR DRIFT: {name} matched {src.count(marker)} times "
                  f"(expected 1) in {target}. Upstream simple_failover.py "
                  f"changed -- update patch_simple_failover.py.", file=sys.stderr)
            return 1
    out = src.replace(T_ANCHOR, T_HELPER + T_ANCHOR, 1).replace(T_OLD, T_NEW, 1)
    compile(out, str(target), "exec")
    _write(target, out, crlf)
    print(f"  patched (stt timeout knob): {target}")
    return 0


MARKER_TTS_TIMEOUT = "TTS TIMEOUT KNOB (voicemode-local)"

# Anchor: the helper goes immediately before the function that builds every TTS client.
TT_ANCHOR = "def _prepare_tts_endpoint(base_url, voice, model, clone_profile):\n"

TT_HELPER = '''# TTS TIMEOUT KNOB (voicemode-local): upstream builds every TTS client with a hardcoded
# 30s timeout, and local endpoints get no SDK retries. A local Kokoro under CPU load can
# need longer for one long sentence (Windows, 2026-11-13, n=1: 12.4s of audio took 40.9s),
# so the request times out and the turn is lost. Streaming is off while barge-in may
# listen, so the whole sentence is generated before playback starts.
# Defaults are unchanged (30s everywhere); a deployment opts in with:
#   VOICEMODE_TTS_TIMEOUT        seconds, all TTS endpoints (default 30)
#   VOICEMODE_TTS_TIMEOUT_LOCAL  seconds, local endpoints only (default: as above)
# A value that is not a number is logged and the default is used.
def _tts_timeout(base_url: str) -> float:
    import logging
    import os

    def _num(name: str, fallback: float) -> float:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return fallback
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if value <= 0:
            logging.getLogger("voicemode").warning(
                "%s=%r is not a positive number of seconds; using %ss", name, raw, fallback)
            return fallback
        return value

    default = _num("VOICEMODE_TTS_TIMEOUT", 30.0)
    if is_local_provider(base_url):
        return _num("VOICEMODE_TTS_TIMEOUT_LOCAL", default)
    return default


'''

TT_OLD = "        timeout=30.0,  # Reasonable timeout\n"
TT_NEW = "        timeout=_tts_timeout(base_url),  # voicemode-local: see _tts_timeout\n"


def apply_tts_timeout_knob(target: Path) -> int:
    """Edit 3: make the TTS client's 30s timeout configurable."""
    src, crlf = _read(target)
    if MARKER_TTS_TIMEOUT in src:
        print(f"  already patched (tts timeout knob): {target}")
        return 0
    for name, marker in (("tts helper anchor", TT_ANCHOR), ("tts timeout literal", TT_OLD)):
        if src.count(marker) != 1:
            print(f"ANCHOR DRIFT: {name} matched {src.count(marker)} times "
                  f"(expected 1) in {target}. Upstream simple_failover.py "
                  f"changed -- update patch_simple_failover.py.", file=sys.stderr)
            return 1
    out = src.replace(TT_ANCHOR, TT_HELPER + TT_ANCHOR, 1).replace(TT_OLD, TT_NEW, 1)
    compile(out, str(target), "exec")
    _write(target, out, crlf)
    print(f"  patched (tts timeout knob): {target}")
    return 0


def apply(target: Path) -> int:
    src, crlf = _read(target)
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
    _write(target, out, crlf)
    print(f"  patched (no silent swap): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "simple_failover.py"
    # Both edits always run: they are independent, so drift in one must not
    # silently skip the other. Exit non-zero if either failed.
    rc_swap = apply(target)
    rc_timeout = apply_timeout_knob(target)
    rc_tts = apply_tts_timeout_knob(target)
    sys.exit(rc_swap or rc_timeout or rc_tts)
