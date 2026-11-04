"""MCP tool so any client can switch voice barge-in on and off by itself.

voice-mode's update_config can write the setting but cannot start or stop the
trigger process, so this goes through voicemode-switch.

Installed into voice_mode/tools/voice_barge_tool.py by patches/apply.sh.
"""
import asyncio

from voice_mode.server import mcp
from voice_mode.voice_barge import barge_control


@mcp.tool()
async def voice_barge(action: str = "status") -> str:
    """Turn voice interruption on or off: whether talking over the assistant stops its speech.

    Use when the user asks to enable, disable or check interrupting by voice.

    Args:
        action: "on", "off" or "status".
    """
    return await asyncio.to_thread(barge_control, action)
