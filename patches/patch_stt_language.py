#!/usr/bin/env python3
"""Patch voice_mode/simple_failover.py: the STT request follows the pill's language.

Whisper guesses the language of every recording unless told, and Stephan saw a transcription
"in another language". When the pill's language for this session is set -- its override,
else the pill's last choice, the same resolver as the voice
(voice_pill_state.pill_choice(os.getpid())["language"], a name such as "German") --
simple_stt_failover passes its ISO 639-1 code (voice_languages.iso_code) as the request's
`language`, to local Whisper and to OpenAI alike. Unset, unknown, or unreadable: today's
behaviour (VOICEMODE_WHISPER_LANGUAGE, else auto-detect). voice_pill_state is only read.

Needs voice_pill_state.py and voice_languages.py copied into the package first. Idempotent;
on drift prints ANCHOR DRIFT, exits 1 and leaves the file untouched. Verified against
voice-mode 8.12.0.

Usage: patch_stt_language.py <path-to-simple_failover.py>
"""
import sys
from pathlib import Path

MARKER = "STT LANGUAGE (voicemode-local)"

HELPER = '''# STT LANGUAGE (voicemode-local): the language the user chose in the pill for this
# session, as an ISO 639-1 code for the STT request, or None (Whisper detects, as before).
def _vml_stt_language():
    try:
        import os
        from . import voice_pill_state
        from .voice_languages import iso_code
        # The voice's own resolver: this session's override, else the pill's last choice.
        name = voice_pill_state.pill_choice(os.getpid()).get("language")
    except Exception as error:  # the pill's state must never break speech recognition
        logger.debug(f"STT: could not read the pill's language: {error}")
        return None
    code = iso_code(name) if name else None
    if name and not code:
        logger.info(f"STT: the pill's language {name!r} has no ISO code; Whisper detects the language")
    return code


'''

PAIRS = [
    ("async def simple_stt_failover(\n", HELPER + "async def simple_stt_failover(\n"),
    ('            if WHISPER_LANGUAGE and WHISPER_LANGUAGE != "auto":\n',
     "            _vml_language = _vml_stt_language()  # STT LANGUAGE (voicemode-local)\n"
     "            if _vml_language:\n"
     "                # The pill's language for this session: Whisper does not guess.\n"
     '                transcription_kwargs["language"] = _vml_language\n'
     '            elif WHISPER_LANGUAGE and WHISPER_LANGUAGE != "auto":\n'),
]


def _read(target: Path):
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched (stt language): {target}")
        return 0
    for old, _ in PAIRS:
        if src.count(old) != 1:
            print(f"ANCHOR DRIFT: {old.strip()!r} matched {src.count(old)} times (expected 1) in "
                  f"{target}. Upstream simple_failover.py changed -- update patch_stt_language.py.",
                  file=sys.stderr)
            return 1
    for old, new in PAIRS:
        src = src.replace(old, new, 1)
    compile(src, str(target), "exec")
    _write(target, src, crlf)
    print(f"  patched (stt language): {target}")
    return 0


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    target = Path(argv[1])
    if target.name != "simple_failover.py":
        print(f"ERROR: patch_stt_language.py patches simple_failover.py, not {target.name}", file=sys.stderr)
        return 2
    return apply(target)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
