#!/usr/bin/env python3
"""Only voice settings travel from voicemode.env into the voice server's environment.

Upstream's update_config MCP tool writes any uppercase key into voicemode.env, and
load_voicemode_env (config.py, on start and on config_reload) copies every key into
os.environ, which every child process inherits. So a model with only the voice tools
could set BASH_ENV=$(cmd), and cmd ran when the server next started voicemode-switch, a
bash script (final review 2026-11-12, VERIFIED with bash; the full chain INFERRED).

The loader now copies a key only when it is a voice setting: VOICEMODE_*, or the legacy
VOICE_MODE_AUTO_START_KOKORO. update_config refuses any other key, so the file never gets
one. A model provider's key is not allowed here; patch_env_provider_keys.py (optional, in
manifest.json only) adds the two that upstream reads, for installs that keep one in
voicemode.env. FELIX never sets a provider key and leaves that patch out. The real environment is untouched: a user's own variables still work.
Settings that name a program are, in addition, trusted from the environment only
(voice_pill_state.trusted_env).

Idempotent (marker). Stops setup if an anchor is not found exactly once. Verified against
pristine voice-mode 8.12.0.
"""
import sys
from pathlib import Path

MARKER = "VML: ENV ALLOWLIST"

EXTRA_KEYS_LINE = "_VML_EXTRA_ENV_KEYS = (\"VOICE_MODE_AUTO_START_KOKORO\",)  # " + MARKER + "\n"

HELPER = (
    EXTRA_KEYS_LINE
    + "\n\n"
    "def _vml_env_key_allowed(key):  # " + MARKER + "\n"
    "    \"\"\"A voice setting may come from voicemode.env; anything else (BASH_ENV, PATH,\n"
    "    LD_PRELOAD, ...) would reach every child process and is refused.\"\"\"\n"
    "    return key.startswith(\"VOICEMODE_\") or key in _VML_EXTRA_ENV_KEYS\n"
    "\n"
    "\n"
)

CONFIG_PAIRS = [
    ("def load_voicemode_env():\n",
     HELPER + "def load_voicemode_env():\n"),
    ("                        if key and key not in os.environ:\n"
     "                            os.environ[key] = value\n",
     "                        if key and key not in os.environ and _vml_env_key_allowed(key):\n"
     "                            os.environ[key] = value\n"),
]

MANAGEMENT_PAIRS = [
    # Above the decorator: between @mcp.tool() and the function it would become the tool.
    ("@mcp.tool()\nasync def update_config(key: str, value: str) -> str:\n",
     HELPER + "@mcp.tool()\nasync def update_config(key: str, value: str) -> str:\n"),
    ("    # Use user config path, check for legacy if new doesn't exist\n"
     "    config_path = USER_CONFIG_PATH\n",
     "    if not _vml_env_key_allowed(key):  # " + MARKER + "\n"
     "        return (f\"❌ {key} is not a voice setting. Only VOICEMODE_* keys can be set \"\n"
     "                f\"here; anything else would reach every program the voice server starts. \"\n"
     "                f\"Set other variables in the real environment.\")\n"
     "\n"
     "    # Use user config path, check for legacy if new doesn't exist\n"
     "    config_path = USER_CONFIG_PATH\n"),
]


def _read(target):
    raw = target.read_bytes()
    ending = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8").replace("\r\n", "\n"), ending


def _write(target, text, ending):
    target.write_bytes(text.replace("\n", ending).encode("utf-8"))


def apply(target):
    source, ending = _read(target)
    if target.name == "config.py":
        pairs = CONFIG_PAIRS
    elif target.name == "configuration_management.py":
        pairs = MANAGEMENT_PAIRS
    else:
        print(f"[patch_env_allowlist] {target}: only config.py and tools/configuration_management.py "
              "are patched", file=sys.stderr)
        return 1
    if MARKER in source:
        if any(r not in source for _, r in pairs):
            print(f"[{Path(__file__).name}] {target}: carries an older version of this patch; it is "
                  "kept. To get the new text, reinstall voice-mode and apply again "
                  "(docs/patching/README.md).", file=sys.stderr)
        return 0
    for anchor, _ in pairs:
        if source.count(anchor) != 1:
            print(f"[patch_env_allowlist] {target}: an anchor was found {source.count(anchor)} times, "
                  "not once", file=sys.stderr)
            return 1
    for anchor, replacement in pairs:
        source = source.replace(anchor, replacement)
    compile(source, str(target), "exec")
    _write(target, source, ending)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: patch_env_allowlist.py <path to config.py or tools/configuration_management.py>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(apply(Path(sys.argv[1])))
