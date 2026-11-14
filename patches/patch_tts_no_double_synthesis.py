#!/usr/bin/env python3
"""Patch voice_mode core.py and streaming.py: a timed-out local TTS request is not sent again.

Upstream's text_to_speech tries streaming first; when that fails for any reason it sends
the same text again on the buffered path. After a timeout against a local server that is
the worst choice: the server is still generating the first request (voicemode-local's
Kokoro server answers only when the whole audio is ready), so it now generates the same
speech twice, and the turn waits two full timeouts. Windows, 2026-11-14: a 1,178-character
reply waited 90 s + 90 s, the server generated 54.4 s of audio in 134.7 s and again in
169.6 s, and the reply was never spoken.

Two edits:

  streaming.py  the PCM and buffered streaming paths keep the exception they caught on
                their metrics (metrics.vml_error) instead of only logging it;
  core.py       when that exception is a timeout and the endpoint is local, raise a
                timeout with a plain message instead of falling back to buffered playback.

Every other streaming failure still falls back, as upstream. Cloud endpoints are not
changed. Idempotent per file; on drift prints ANCHOR DRIFT, exits 1 and leaves the file
untouched. Verified against voice-mode 8.12.0, alone and after every patch in
manifest.json and manifest.windows.json.

Usage: patch_tts_no_double_synthesis.py <path-to-core.py | path-to-streaming.py>
"""
import sys
from pathlib import Path

MARKER = "TTS NO DOUBLE SYNTHESIS (voicemode-local)"

# --- streaming.py -------------------------------------------------------------------------

S_EDITS = [
    ('        logger.error(f"PCM streaming failed: {e}")\n        return False, metrics\n',
     '        logger.error(f"PCM streaming failed: {e}")\n'
     '        metrics.vml_error = e  # TTS NO DOUBLE SYNTHESIS (voicemode-local): core.py reads it\n'
     '        return False, metrics\n'),
    ('        logger.error(f"Buffered streaming failed: {e}")\n        return False, metrics\n',
     '        logger.error(f"Buffered streaming failed: {e}")\n'
     '        metrics.vml_error = e  # TTS NO DOUBLE SYNTHESIS (voicemode-local): core.py reads it\n'
     '        return False, metrics\n'),
]

# --- core.py ------------------------------------------------------------------------------

C_HELPER_ANCHOR = "async def text_to_speech(\n"
C_HELPER = '''# TTS NO DOUBLE SYNTHESIS (voicemode-local): after a streaming request to a LOCAL server
# timed out, do not send the same text again buffered. The server is still generating the
# first request, so a second one makes it generate the same speech twice and the turn waits
# two full timeouts (Windows 2026-11-14: 90 s + 90 s, nothing spoken). Other streaming
# failures still fall back, as upstream. See patches/patch_tts_no_double_synthesis.py.
class VmlTtsTimeout(TimeoutError):
    """A local TTS request timed out and was deliberately not sent again."""


def _vml_is_tts_timeout(error) -> bool:
    kinds = [httpx.TimeoutException, TimeoutError, asyncio.TimeoutError]
    try:
        from openai import APITimeoutError
        kinds.append(APITimeoutError)
    except Exception:
        pass
    return isinstance(error, tuple(kinds))


def _vml_tts_timeout_error(client) -> "VmlTtsTimeout":
    limit = getattr(client, "timeout", None)
    seconds = limit if isinstance(limit, (int, float)) else getattr(limit, "read", None)
    after = f" after {seconds:g} s" if isinstance(seconds, (int, float)) else ""
    return VmlTtsTimeout(
        f"Request timed out{after}. Not sent again without streaming: the local speech "
        "service would generate the same speech a second time.")


'''

C_FALLBACK_ANCHOR = '                logger.warning("Streaming failed, falling back to buffered playback")\n'
C_FALLBACK = (
    '                # TTS NO DOUBLE SYNTHESIS (voicemode-local): see _vml_is_tts_timeout\n'
    '                _vml_stream_error = getattr(stream_metrics, "vml_error", None)\n'
    '                if (_vml_stream_error is not None and _vml_is_tts_timeout(_vml_stream_error)\n'
    '                        and is_local_provider(tts_base_url)):\n'
    '                    logger.warning("Streaming TTS to a local server timed out; not sending the "\n'
    '                                   "same text again buffered")\n'
    '                    raise _vml_tts_timeout_error(openai_clients.get(client_key)) from _vml_stream_error\n'
    + C_FALLBACK_ANCHOR)


def _read(target: Path):
    """The file as LF text, and whether it used CRLF. UTF-8 whatever the locale."""
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def _drift(name: str, found: int, target: Path) -> int:
    print(f"ANCHOR DRIFT: {name} matched {found} times (expected 1) in {target}. Upstream "
          f"{target.name} changed -- update patch_tts_no_double_synthesis.py.", file=sys.stderr)
    return 1


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched (tts no double synthesis): {target}")
        return 0
    if target.name == "streaming.py":
        edits = S_EDITS
    elif target.name == "core.py":
        edits = [(C_HELPER_ANCHOR, C_HELPER + C_HELPER_ANCHOR), (C_FALLBACK_ANCHOR, C_FALLBACK)]
        for name in ("import asyncio\n", "import httpx\n", "from .provider_discovery import is_local_provider\n"):
            if name not in src:
                return _drift(repr(name.strip()), 0, target)
    else:
        print(f"ERROR: patch_tts_no_double_synthesis.py patches core.py or streaming.py, "
              f"not {target.name}", file=sys.stderr)
        return 2
    out = src
    for old, new in edits:
        if out.count(old) != 1:
            return _drift(repr(old.strip().splitlines()[0]), out.count(old), target)
        out = out.replace(old, new, 1)
    compile(out, str(target), "exec")
    _write(target, out, crlf)
    print(f"  patched (tts no double synthesis): {target}")
    return 0


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    return apply(Path(argv[1]))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
