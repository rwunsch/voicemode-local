"""Switch-mode prompt for VoiceMode (patched by voicemode-local).

Registers /voicemode:switch-mode in Claude Code's slash menu.
"""

from voice_mode.server import mcp


@mcp.prompt(name="switch-mode")
def switch_mode_prompt() -> str:
    """Switch between voice engine modes (local, piper, openai, hybrid)."""
    return """Switch the VoiceMode TTS/STT engine. Available modes:

- **local**: Kokoro TTS + local Whisper STT (free, private)
- **piper**: Piper TTS + local Whisper STT (free, multilingual — German, Dutch, etc.)
- **openai**: OpenAI cloud TTS + STT (best quality, ~$0.01/min)
- **hybrid**: Local Kokoro TTS + OpenAI cloud STT (~$0.006/min)

Ask the user which mode they want, then call the `switch_mode` tool with that mode.
After switching, inform the user they need to restart Claude Code for changes to take effect."""
