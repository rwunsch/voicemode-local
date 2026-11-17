#!/usr/bin/env python3
"""Patch voice_mode: an uninstalled voice is spoken with an installed one (voice_fallback.py).

simple_failover.py: simple_tts_failover and simple_tts_synthesize (whatever they are by then,
tts_chunks' wrapper included) are wrapped at the end of the file by voice_fallback.with_fallback:
when every local endpoint rejected the voice as unknown, the request is sent once more with an
installed voice of the same language. Any other failure returns today's result.

tools/converse.py: _converse_core is wrapped at the end of the file so the converse result
says "Voice X is not installed here; spoke with Y." when that happened. converse() looks
_converse_core up when it runs, so every return path gets the note.

Why, and the rules for choosing the voice (local only, never OpenAI): voice_fallback.py.
Needs voice_languages.py and voice_fallback.py copied into the package first (manifest steps
before this one). Idempotent; on drift prints ANCHOR DRIFT, exits 1 and leaves the file
untouched. Verified against voice-mode 8.12.0.

Usage: patch_voice_fallback.py <path-to-simple_failover.py | path-to-tools/converse.py>
"""
import sys
from pathlib import Path

MARKER = "VOICE FALLBACK (voicemode-local)"

FAILOVER_ANCHORS = ("async def simple_tts_failover(\n", "async def simple_tts_synthesize(\n")
FAILOVER_WRAPPER = '''

# VOICE FALLBACK (voicemode-local): when every local TTS server rejected the voice as unknown,
# speak once more with an installed voice of the same language (never an OpenAI voice), and
# keep today's result for any other failure. See voice_mode/voice_fallback.py.
_vml_simple_tts_failover_one_voice = simple_tts_failover
_vml_simple_tts_synthesize_one_voice = simple_tts_synthesize


def _vml_voice_fallback_applies(voice) -> bool:
    try:
        from .voice_profiles import is_clone_voice
        return not is_clone_voice(voice)   # a clone voice has its own endpoint: no stand-in
    except Exception:
        return True


def _vml_voice_fallback_options() -> Dict[str, Any]:
    from .config import TTS_VOICES
    return {"base_urls": list(TTS_BASE_URLS or []), "configured": list(TTS_VOICES or []),
            "is_local": is_local_provider}


async def simple_tts_failover(
    text: str,
    voice: str,
    model: Optional[str] = None,
    **kwargs
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not _vml_voice_fallback_applies(voice):
        return await _vml_simple_tts_failover_one_voice(text, voice, model, **kwargs)
    from .voice_fallback import with_fallback
    return await with_fallback(_vml_simple_tts_failover_one_voice, text, voice, model, kwargs,
                               **_vml_voice_fallback_options())


async def simple_tts_synthesize(
    text: str,
    voice: str,
    model: Optional[str] = None,
    **kwargs
):
    if not _vml_voice_fallback_applies(voice):
        return await _vml_simple_tts_synthesize_one_voice(text, voice, model, **kwargs)
    from .voice_fallback import with_fallback
    return await with_fallback(_vml_simple_tts_synthesize_one_voice, text, voice, model, kwargs,
                               **_vml_voice_fallback_options())
'''

CONVERSE_ANCHORS = ("async def _converse_core(\n",)
CONVERSE_WRAPPER = '''

# VOICE FALLBACK (voicemode-local): a turn whose voice was not installed and was spoken with
# another says so in the converse result. See voice_mode/voice_fallback.py.
_vml_converse_core_without_voice_notes = _converse_core


async def _converse_core(*args, **kwargs):
    from ..voice_fallback import notes_scope, suffix
    with notes_scope() as notes:
        result = await _vml_converse_core_without_voice_notes(*args, **kwargs)
    return result + suffix(notes) if isinstance(result, str) else result
'''

TARGETS = {"simple_failover.py": (FAILOVER_ANCHORS, FAILOVER_WRAPPER),
           "converse.py": (CONVERSE_ANCHORS, CONVERSE_WRAPPER)}


def _read(target: Path):
    """The file as LF text, and whether it used CRLF. UTF-8 whatever the locale."""
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply(target: Path) -> int:
    anchors, wrapper = TARGETS[target.name]
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched (voice fallback): {target}")
        return 0
    for anchor in anchors:
        if src.count(anchor) != 1:
            print(f"ANCHOR DRIFT: {anchor.strip()!r} matched {src.count(anchor)} times (expected 1) "
                  f"in {target}. Upstream changed -- update patch_voice_fallback.py.", file=sys.stderr)
            return 1
    out = src.rstrip("\n") + "\n" + wrapper
    compile(out, str(target), "exec")
    _write(target, out, crlf)
    print(f"  patched (voice fallback): {target}")
    return 0


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    target = Path(argv[1])
    if target.name not in TARGETS:
        print(f"ERROR: patch_voice_fallback.py patches simple_failover.py and converse.py, "
              f"not {target.name}", file=sys.stderr)
        return 2
    return apply(target)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
