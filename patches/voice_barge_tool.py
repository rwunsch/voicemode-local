"""MCP tools any client can call: voice_barge and voice_stop.

voice-mode's update_config can write the barge setting but cannot start or stop
the trigger process, so voice_barge goes through voicemode-switch.

Installed into voice_mode/tools/voice_barge_tool.py by patches/apply.sh.
"""
import asyncio
import os

from voice_mode import voice_pill_state
from voice_mode.server import mcp
from voice_mode.voice_barge import barge_control


@mcp.tool()
async def voice_barge(action: str = "status") -> str:
    """Turn voice interruption on or off: whether talking over the assistant stops its speech.

    Use when the user asks to enable, disable or check interrupting by voice.

    Args:
        action: "on", "off", "auto" or "status". auto listens only while the sound plays
            to headphones or a headset, decided at each reply.
    """
    return await asyncio.to_thread(barge_control, action)


@mcp.tool()
async def voice_stop() -> str:
    """End voice mode for this session and remove it from the voice pill.

    Call this when the user says "stop voice mode", then continue in text and do not
    call converse again unless the user asks for voice.
    """
    voice_pill_state.unregister_session(os.getpid())
    return "Voice mode stopped for this session."
