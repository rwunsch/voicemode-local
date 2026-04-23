"""Mode switching tool for VoiceMode (patched by voicemode-local)."""

import json
import os
from voice_mode.server import mcp

MODES = {
    "local": {
        "description": "Local Whisper STT + Kokoro TTS (free, private)",
        "STT_BASE_URL": "http://127.0.0.1:2022/v1",
        "TTS_BASE_URL": "http://127.0.0.1:8880/v1",
        "TTS_VOICE": "af_sky",
    },
    "piper": {
        "description": "Local Whisper STT + Piper TTS (free, multilingual)",
        "STT_BASE_URL": "http://127.0.0.1:2022/v1",
        "TTS_BASE_URL": "http://127.0.0.1:8881/v1",
        "TTS_VOICE": "p_de_thorsten",
    },
    "openai": {
        "description": "OpenAI cloud STT + TTS (best quality, ~$0.01/min)",
        "STT_BASE_URL": "",
        "TTS_BASE_URL": "",
        "TTS_VOICE": "",
    },
    "hybrid": {
        "description": "OpenAI STT + local Kokoro TTS (~$0.006/min)",
        "STT_BASE_URL": "",
        "TTS_BASE_URL": "http://127.0.0.1:8880/v1",
        "TTS_VOICE": "af_sky",
    },
}


@mcp.tool()
def switch_mode(mode: str) -> str:
    """Switch VoiceMode between local, piper, openai, and hybrid modes.

    Changes the STT/TTS configuration in ~/.claude.json.
    After switching, Claude Code needs to reconnect the MCP server
    for the new settings to take effect.

    Args:
        mode: One of 'local', 'piper', 'openai', 'hybrid'
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
    openai_key = env.get("OPENAI_API_KEY", "")

    new_env = {"OPENAI_API_KEY": openai_key}
    for key in ("STT_BASE_URL", "TTS_BASE_URL", "TTS_VOICE"):
        value = config[key]
        if value:
            new_env[key] = value

    data["mcpServers"]["voicemode"]["env"] = new_env

    with open(claude_json, "w") as f:
        json.dump(data, f, indent=2)

    return (
        f"Switched to {mode} mode: {config['description']}\n\n"
        f"Settings updated in ~/.claude.json.\n"
        f"Please restart Claude Code for changes to take effect."
    )
