#!/usr/bin/env python3
"""Patch voice_mode/tools/converse.py: the echo lines' labels come from settings.

converse's tool description asks the agent to echo each turn as
`> **ASSISTANT (voicemode):** <message>` and `> **USER (voicemode):** <reply>`. FELIX wants the
user's name and "FELIX" there. A decorator, applied before @mcp.tool() registers the tool,
puts VOICEMODE_ECHO_ASSISTANT_LABEL and VOICEMODE_ECHO_USER_LABEL into both docstrings (the
tool's and _converse_core's, which repeats it). Unset or blank, the text stays upstream's to
the byte. The settings are VOICEMODE_*, so they pass the env allowlist from voicemode.env;
they are read when voice-mode starts (a change needs a reconnect).

Idempotent; on drift prints ANCHOR DRIFT, exits 1 and leaves the file untouched. Verified
against voice-mode 8.12.0.

Usage: patch_echo_labels.py <path-to-tools/converse.py>
"""
import sys
from pathlib import Path

MARKER = "ECHO LABELS (voicemode-local)"

HELPER = '''# ECHO LABELS (voicemode-local): the transcript echo lines' labels, from
# VOICEMODE_ECHO_ASSISTANT_LABEL and VOICEMODE_ECHO_USER_LABEL (default ASSISTANT and USER).
# Applied to the docstring before @mcp.tool() reads it as the tool description.
def _vml_echo_label(name, default):
    import os
    import re
    value = re.sub(r"[\\r\\n*`]", "", os.getenv(name) or "").strip()
    return value or default


def _vml_echo_labels(fn):
    assistant = _vml_echo_label("VOICEMODE_ECHO_ASSISTANT_LABEL", "ASSISTANT")
    user = _vml_echo_label("VOICEMODE_ECHO_USER_LABEL", "USER")
    if fn.__doc__ and (assistant, user) != ("ASSISTANT", "USER"):
        fn.__doc__ = (fn.__doc__
                      .replace("**ASSISTANT (voicemode):**", f"**{assistant} (voicemode):**")
                      .replace("**USER (voicemode):**", f"**{user} (voicemode):**"))
    return fn


'''

PAIRS = [
    ("async def _converse_core(\n", HELPER + "@_vml_echo_labels\nasync def _converse_core(\n"),
    ("@mcp.tool()\nasync def converse(\n", "@mcp.tool()\n@_vml_echo_labels\nasync def converse(\n"),
]


def _read(target: Path):
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src:
        print(f"  already patched (echo labels): {target}")
        return 0
    for old, _ in PAIRS:
        if src.count(old) != 1:
            print(f"ANCHOR DRIFT: {old.strip()!r} matched {src.count(old)} times (expected 1) in "
                  f"{target}. Upstream converse.py changed -- update patch_echo_labels.py.", file=sys.stderr)
            return 1
    for old, new in PAIRS:
        src = src.replace(old, new, 1)
    compile(src, str(target), "exec")
    _write(target, src, crlf)
    print(f"  patched (echo labels): {target}")
    return 0


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    target = Path(argv[1])
    if target.name != "converse.py":
        print(f"ERROR: patch_echo_labels.py patches converse.py, not {target.name}", file=sys.stderr)
        return 2
    return apply(target)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
