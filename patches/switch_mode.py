"""Mode switching tool for VoiceMode (patched by voicemode-local).

IMPORTANT: voice-mode only reads the *plural* endpoint-list env vars
(VOICEMODE_TTS_BASE_URLS / VOICEMODE_STT_BASE_URLS / VOICEMODE_VOICES). The
older singular TTS_BASE_URL / STT_BASE_URL / TTS_VOICE are NOT read by this
version, so writing them is a silent no-op. Always write the plural lists.

Routing is priority-ordered: a requested voice goes to the first endpoint that
serves it (each local engine rejects voices it does not own in a few ms), so
listing Kokoro + Piper before OpenAI makes both locals equal first-class
citizens and keeps OpenAI strictly last-resort.
"""

import json
import os
import re
from voice_mode.server import mcp

VOICEMODE_ENV = os.path.expanduser("~/.voicemode/voicemode.env")
_BEGIN = "# >>> voicemode-switch managed (do not edit inside this block) >>>"
_END = "# <<< voicemode-switch managed <<<"


def _write_voicemode_env(mode: str, stt: str, tts: str, voices: str) -> None:
    """Write the routing config as a managed block in ~/.voicemode/voicemode.env
    (the stable source of truth voice-mode loads at startup). Env vars override
    this file, so routing must NOT also live in ~/.claude.json."""
    os.makedirs(os.path.dirname(VOICEMODE_ENV), exist_ok=True)
    txt = ""
    if os.path.exists(VOICEMODE_ENV):
        txt = open(VOICEMODE_ENV).read()
        txt = re.sub(re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?", "", txt, flags=re.S)
    block = [_BEGIN, f"# mode: {mode}"]
    if stt:
        block.append(f"VOICEMODE_STT_BASE_URLS={stt}")
    if tts:
        block.append(f"VOICEMODE_TTS_BASE_URLS={tts}")
    if voices:
        block.append(f"VOICEMODE_VOICES={voices}")
    block.append(_END)
    body = txt.rstrip("\n")
    open(VOICEMODE_ENV, "w").write((body + "\n\n" if body else "") + "\n".join(block) + "\n")
    os.chmod(VOICEMODE_ENV, 0o600)


def _strip_claude_json_routing() -> None:
    """Remove routing vars from ~/.claude.json so they don't override
    voicemode.env. Keeps OPENAI_API_KEY and a key-only WSLENV."""
    p = os.path.expanduser("~/.claude.json")
    if not os.path.exists(p):
        return
    data = json.load(open(p))
    vm = data.get("mcpServers", {}).get("voicemode")
    if not vm:
        return
    env = vm.get("env", {})
    for k in ("VOICEMODE_STT_BASE_URLS", "VOICEMODE_TTS_BASE_URLS", "VOICEMODE_VOICES",
              "STT_BASE_URL", "TTS_BASE_URL", "TTS_VOICE"):
        env.pop(k, None)
    if "WSLENV" in env:
        keep = [x for x in env["WSLENV"].split(":") if x.startswith("OPENAI_API_KEY")]
        env["WSLENV"] = ":".join(keep)
        if not env["WSLENV"]:
            env.pop("WSLENV")
    vm["env"] = env
    json.dump(data, open(p, "w"), indent=2)

KOKORO = "http://127.0.0.1:8880/v1"
PIPER = "http://127.0.0.1:8881/v1"
WHISPER = "http://127.0.0.1:2022/v1"
OPENAI = "https://api.openai.com/v1"

MODES = {
    "local": {
        "description": "Local Kokoro+Piper TTS & Whisper STT, OpenAI last resort (recommended)",
        "VOICEMODE_STT_BASE_URLS": f"{WHISPER},{OPENAI}",
        "VOICEMODE_TTS_BASE_URLS": f"{KOKORO},{PIPER},{OPENAI}",
        "VOICEMODE_VOICES": "af_sky",
    },
    "localonly": {
        "description": "Local Kokoro+Piper+Whisper only, no cloud (fails loud if down)",
        "VOICEMODE_STT_BASE_URLS": WHISPER,
        "VOICEMODE_TTS_BASE_URLS": f"{KOKORO},{PIPER}",
        "VOICEMODE_VOICES": "af_sky",
    },
    "piper": {
        "description": "Piper TTS primary (German etc.), Kokoro + OpenAI behind",
        "VOICEMODE_STT_BASE_URLS": f"{WHISPER},{OPENAI}",
        "VOICEMODE_TTS_BASE_URLS": f"{PIPER},{KOKORO},{OPENAI}",
        "VOICEMODE_VOICES": "p_de_thorsten",
    },
    "openai": {
        "description": "OpenAI cloud STT + TTS (best quality, ~$0.01/min)",
        "VOICEMODE_STT_BASE_URLS": OPENAI,
        "VOICEMODE_TTS_BASE_URLS": OPENAI,
        "VOICEMODE_VOICES": "nova",
    },
    "hybrid": {
        "description": "OpenAI STT + local Kokoro+Piper TTS (~$0.006/min)",
        "VOICEMODE_STT_BASE_URLS": f"{OPENAI},{WHISPER}",
        "VOICEMODE_TTS_BASE_URLS": f"{KOKORO},{PIPER},{OPENAI}",
        "VOICEMODE_VOICES": "af_sky",
    },
}

_LIST_KEYS = ("VOICEMODE_STT_BASE_URLS", "VOICEMODE_TTS_BASE_URLS", "VOICEMODE_VOICES")


@mcp.tool()
def switch_mode(mode: str) -> str:
    """Switch VoiceMode between local, localonly, piper, openai, and hybrid modes.

    Changes the STT/TTS endpoint lists in ~/.claude.json. After switching,
    Claude Code needs to reconnect the MCP server for the new settings to
    take effect.

    Args:
        mode: One of 'local', 'localonly', 'piper', 'openai', 'hybrid'
    """
    if mode not in MODES:
        available = ", ".join(MODES.keys())
        return f"Unknown mode: {mode}. Available modes: {available}"

    config = MODES[mode]

    try:
        _write_voicemode_env(
            mode,
            config["VOICEMODE_STT_BASE_URLS"],
            config["VOICEMODE_TTS_BASE_URLS"],
            config["VOICEMODE_VOICES"],
        )
        _strip_claude_json_routing()
    except Exception as e:  # pragma: no cover - defensive
        return f"Error writing config: {e}"

    tts = config["VOICEMODE_TTS_BASE_URLS"]
    stt = config["VOICEMODE_STT_BASE_URLS"]

    def label(urls):
        parts = []
        if "8880" in urls:
            parts.append("Kokoro")
        if "8881" in urls:
            parts.append("Piper")
        if "2022" in urls:
            parts.append("Whisper")
        if "openai.com" in urls:
            parts.append("OpenAI")
        return " -> ".join(parts) if parts else "(none)"

    return (
        f"Switched to {mode} mode: {config['description']}\n\n"
        f"Current config (priority order):\n"
        f"  STT: {label(stt)}\n"
        f"  TTS: {label(tts)}\n"
        f"  Default voice: {config['VOICEMODE_VOICES']}\n\n"
        f"Settings written to ~/.voicemode/voicemode.env.\n"
        f"Please restart Claude Code for changes to take effect."
    )
