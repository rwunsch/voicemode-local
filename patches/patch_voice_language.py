#!/usr/bin/env python3
"""Tell the LLM, in the converse tool description, which voices are language-locked.

The gap
=======
Most local voices are tied to one language. ``dm_martin`` (German Kokoro
fine-tune, docs/german-kokoro/) and Piper's ``p_de_*`` voices phonemize every
input as German, so English text sent to them comes out garbled. The
stock Kokoro voices (``af_*``, ``bm_*`` ...) do the same for English. Nothing in
upstream's tool description says so, so an agent that keeps the German voice
while the conversation drifts to English produces something unintelligible.
This happened on 2026-10-19.

The CLAUDE.md rule in this repo only reaches Claude Code sessions that run
inside this checkout. The tool description reaches every MCP client (Claude Code
anywhere, Copilot CLI, VS Code).

The fix
=======
Append a short LANGUAGE-LOCKED VOICES note right after upstream's persona
convention bullet in the converse docstring. That bullet appears twice in
converse.py (two registrations of the tool), and both get the note.

Anchors verified against voice-mode 8.12.0 (2026-10-19).
Idempotent; fails loudly on drift.

Usage: patch_voice_language.py [<path-to-converse.py>]
"""
import sys
from pathlib import Path

MARKER = "LANGUAGE-LOCKED VOICES"

ANCHOR = (
    "     speaking in-character (index: PERSONAS.md). MCP-resource version planned: VM-1222.\n"
)
NOTE = (
    "   - LANGUAGE-LOCKED VOICES (voicemode-local): a voice speaks only its own language.\n"
    "     dm_martin and p_de_* are German: write EVERY message for them in German, even\n"
    "     if the user speaks English. af_*/am_*/bf_*/bm_* are English. If the conversation\n"
    "     changes language, change the voice too. Never send text in another language.\n"
)
EXPECTED = 2


def apply(target: Path) -> int:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched (voice language): {target}")
        return 0
    count = src.count(ANCHOR)
    if count != EXPECTED:
        print(
            f"ANCHOR DRIFT: persona bullet matched {count} times (expected {EXPECTED}) in "
            f"{target}. Upstream converse.py changed — update patches/patch_voice_language.py.",
            file=sys.stderr,
        )
        return 1
    out = src.replace(ANCHOR, ANCHOR + NOTE)
    compile(out, str(target), "exec")  # syntax safety net before writing
    target.write_text(out)
    print(f"  patched (voice language): {target}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
