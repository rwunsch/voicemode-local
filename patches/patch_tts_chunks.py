#!/usr/bin/env python3
"""Patch voice_mode/simple_failover.py: speak a long reply in parts (tts_chunks.py).

Upstream's simple_tts_failover becomes _vml_simple_tts_failover_whole, unchanged, and a
new simple_tts_failover at the end of the file hands it to tts_chunks.speak_in_chunks,
which calls it once per part. With VOICEMODE_TTS_CHUNK_CHARS unset or 0 it is called
once with the whole text, as before. Why, and what a stop or a failed part does:
tts_chunks.py.

Needs tts_chunks.py copied into the package first (manifest step before this one).
Idempotent; on drift prints ANCHOR DRIFT, exits 1 and leaves the file untouched.
Verified against voice-mode 8.12.0.

Usage: patch_tts_chunks.py <path-to-simple_failover.py>
"""
import sys
from pathlib import Path

MARKER = "TTS CHUNKS (voicemode-local)"

ANCHOR = "async def simple_tts_failover(\n"
RENAMED = "async def _vml_simple_tts_failover_whole(  # TTS CHUNKS (voicemode-local): upstream's, renamed\n"

WRAPPER = '''

# TTS CHUNKS (voicemode-local): a long reply is spoken in parts of at most
# VOICEMODE_TTS_CHUNK_CHARS characters, one request each, so no request comes near the
# TTS timeout on a slow machine. Off (one request, as upstream) when unset or 0.
# See voice_mode/tts_chunks.py.
async def simple_tts_failover(
    text: str,
    voice: str,
    model: Optional[str] = None,
    **kwargs
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    from .tts_chunks import speak_in_chunks
    return await speak_in_chunks(_vml_simple_tts_failover_whole, text, voice, model, kwargs)
'''


def _read(target: Path):
    """The file as LF text, and whether it used CRLF. UTF-8 whatever the locale."""
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched (tts chunks): {target}")
        return 0
    if src.count(ANCHOR) != 1:
        print(f"ANCHOR DRIFT: 'async def simple_tts_failover(' matched {src.count(ANCHOR)} times "
              f"(expected 1) in {target}. Upstream simple_failover.py changed -- update "
              "patch_tts_chunks.py.", file=sys.stderr)
        return 1
    out = src.replace(ANCHOR, RENAMED, 1).rstrip("\n") + "\n" + WRAPPER
    compile(out, str(target), "exec")
    _write(target, out, crlf)
    print(f"  patched (tts chunks): {target}")
    return 0


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    target = Path(argv[1])
    if target.name != "simple_failover.py":
        print(f"ERROR: patch_tts_chunks.py patches simple_failover.py, not {target.name}", file=sys.stderr)
        return 2
    return apply(target)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
