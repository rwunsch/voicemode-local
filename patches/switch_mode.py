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
from voice_mode.server import mcp

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
    claude_json = os.path.expanduser("~/.claude.json")

    try:
        with open(claude_json) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return f"Error: Could not read {claude_json}"

    env = data.get("mcpServers", {}).get("voicemode", {}).get("env", {})
    # Preserve OPENAI_API_KEY and any WSLENV passthrough (cross-OS bridge).
    new_env = {"OPENAI_API_KEY": env.get("OPENAI_API_KEY", "")}
    if env.get("WSLENV"):
        new_env["WSLENV"] = env["WSLENV"]
    for key in _LIST_KEYS:
        if config[key]:
            new_env[key] = config[key]

    data["mcpServers"]["voicemode"]["env"] = new_env

    with open(claude_json, "w") as f:
        json.dump(data, f, indent=2)

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
        f"Settings updated in ~/.claude.json.\n"
        f"Please restart Claude Code for changes to take effect."
    )
