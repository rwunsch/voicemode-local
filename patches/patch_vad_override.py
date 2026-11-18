#!/usr/bin/env python3
"""Patch voice_mode/tools/converse.py so VAD aggressiveness and the silence threshold are
resolved at each recording, not frozen at import.

Upstream reads VAD_AGGRESSIVENESS and SILENCE_THRESHOLD_MS once, as module constants
(config.py:948-949), and converse.py imports them by name, so a running voice server never
sees a new value; update_config writes voicemode.env but needs a /mcp reconnect. Robert,
2026-11-18: with background noise, aggressiveness 2 and 2000 ms of silence never ended the
turn, and he had to mute.

Inside record_audio_with_silence_detection, and nowhere else (in its ``_inner`` body when
patch_voice_pill has wrapped it):

1. Upstream's ``effective_vad_aggressiveness = ...`` line becomes a call to
   voice_mode.voice_sensitivity.resolve(vad_aggressiveness, VAD_AGGRESSIVENESS,
   SILENCE_THRESHOLD_MS): this call's argument, else <base dir>/sensitivity.json (written by
   the voice_sensitivity tool, re-read at each recording), else config. If the module is
   missing or anything fails, the values are upstream's, computed as upstream does.
2. Every SILENCE_THRESHOLD_MS in the function after that (the silence exit and its log
   lines) reads the resolved ``_vml_silence_threshold_ms``.

With no override file the values are config's: behaviour as before.

Anchors (each must occur exactly once in the function, else ANCHOR DRIFT, exit 1, nothing
written): the function itself, upstream's effective-aggressiveness line, and the silence
exit's ``silence_duration_ms >= SILENCE_THRESHOLD_MS`` (pristine, or as patch_converse_hold
rewrites it). Idempotent by marker. Keeps the file's line endings. Verified against
voice-mode 8.12.0 pristine and after the whole manifest (2026-11-18).

Usage: patch_vad_override.py [<path-to-converse.py>]
"""
import re
import sys
from pathlib import Path

TAG = "(vad override)"
MARKER = "voicemode-local vad override"
FUNC = "def record_audio_with_silence_detection("
FUNC_INNER = "def record_audio_with_silence_detection_inner("
A_EFFECTIVE = (
    "        effective_vad_aggressiveness = vad_aggressiveness if vad_aggressiveness "
    "is not None else VAD_AGGRESSIVENESS\n"
)
A_SILENCE_EXIT = "silence_duration_ms >= SILENCE_THRESHOLD_MS"
LOCAL = "_vml_silence_threshold_ms"
R_EFFECTIVE = (
    f"        # {MARKER}: aggressiveness and silence length are resolved at\n"
    "        # each recording -- this call's vad_aggressiveness, else <base dir>/sensitivity.json\n"
    "        # (the voice_sensitivity tool), else config -- so a change applies at the next\n"
    "        # listen without a restart. No override file: config's values, as before.\n"
    "        try:\n"
    "            from voice_mode.voice_sensitivity import resolve as _vml_resolve_sensitivity\n"
    f"            effective_vad_aggressiveness, {LOCAL} = _vml_resolve_sensitivity(\n"
    "                vad_aggressiveness, VAD_AGGRESSIVENESS, SILENCE_THRESHOLD_MS)\n"
    "        except Exception:\n"
    "            effective_vad_aggressiveness = vad_aggressiveness if vad_aggressiveness is not None else VAD_AGGRESSIVENESS\n"
    f"            {LOCAL} = SILENCE_THRESHOLD_MS\n"
)
# The function ends where the next top-level statement starts.
_NEXT_TOP = re.compile(r"\n(?=[^\s#\n])")


def _read(target: Path):
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def _drift(target: Path, what: str, count: int) -> int:
    print(f"[patch_vad_override] ANCHOR DRIFT {TAG}: {what} matched {count} times "
          f"(expected exactly 1) in {target}. Upstream voice-mode has likely changed: update "
          f"the anchors in patches/patch_vad_override.py.", file=sys.stderr)
    return 1


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src:
        print(f"[patch_vad_override] {target}: already patched {TAG} -- skipping")
        return 0
    # patch_voice_pill moves upstream's body into ..._inner behind a wrapper: patch that one.
    func = FUNC_INNER if FUNC_INNER in src else FUNC
    if src.count(func) != 1:
        return _drift(target, func[4:-1], src.count(func))
    start = src.index(func)
    nxt = _NEXT_TOP.search(src, start + len(func))
    end = nxt.start() + 1 if nxt else len(src)
    body = src[start:end]
    if body.count(A_EFFECTIVE) != 1:
        return _drift(target, "the effective_vad_aggressiveness line", body.count(A_EFFECTIVE))
    if body.count(A_SILENCE_EXIT) != 1:
        return _drift(target, "the silence exit", body.count(A_SILENCE_EXIT))
    head, tail = body.split(A_EFFECTIVE)
    if "SILENCE_THRESHOLD_MS" in head:
        return _drift(target, "SILENCE_THRESHOLD_MS before the aggressiveness line", head.count("SILENCE_THRESHOLD_MS"))
    new_body = head + R_EFFECTIVE + tail.replace("SILENCE_THRESHOLD_MS", LOCAL)
    out = src[:start] + new_body + src[end:]
    compile(out, str(target), "exec")  # syntax safety net before writing
    _write(target, out, crlf)
    print(f"[patch_vad_override] patched {target} {TAG}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
