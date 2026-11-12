#!/usr/bin/env python3
"""Optional, after patch_env_allowlist.py: let the two model-provider keys that upstream
reads travel from voicemode.env into the environment, as they did before the allowlist.

Listed in manifest.json only (WSL, Linux, macOS installs that may keep a key in
voicemode.env). Not in manifest.windows.json and not in FELIX's lists: FELIX never uses or
sets a model provider's key (Robert, 2026-11-03), and its repo test rejects a vendored file
that names one. On those installs a provider key must come from the real environment.

Idempotent (marker). Needs patch_env_allowlist.py applied first; stops setup otherwise.
"""
import sys
from pathlib import Path

MARKER = "VML: ENV PROVIDER KEYS"
CORE_MARKER = "VML: ENV ALLOWLIST"

ANCHOR = '_VML_EXTRA_ENV_KEYS = ("VOICE_MODE_AUTO_START_KOKORO",)  # ' + CORE_MARKER + "\n"
REPLACEMENT = ('_VML_EXTRA_ENV_KEYS = ("VOICE_MODE_AUTO_START_KOKORO", "OPENAI_API_KEY", '
               '"CARTESIA_API_KEY")  # ' + CORE_MARKER + "; " + MARKER + "\n")


def _read(target):
    raw = target.read_bytes()
    ending = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), ending


def apply(target):
    source, ending = _read(target)
    if target.name not in ("config.py", "configuration_management.py"):
        print(f"[patch_env_provider_keys] {target}: only config.py and "
              "tools/configuration_management.py are patched", file=sys.stderr)
        return 1
    if MARKER in source:
        return 0
    if source.count(ANCHOR) != 1:
        print(f"[patch_env_provider_keys] {target}: patch_env_allowlist.py must be applied first "
              f"(its extra-keys line was found {source.count(ANCHOR)} times, not once)", file=sys.stderr)
        return 1
    source = source.replace(ANCHOR, REPLACEMENT)
    compile(source, str(target), "exec")
    target.write_bytes(source.replace("\n", ending).encode("utf-8"))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_env_provider_keys.py <path to config.py or tools/configuration_management.py>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
