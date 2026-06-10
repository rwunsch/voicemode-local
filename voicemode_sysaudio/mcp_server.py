"""Standalone MCP server exposing the system-audio toggle to a Claude session.

Kept separate from voice-mode (no patch-surface coupling). Register with:

    claude mcp add voicemode-sysaudio -s user -- \
        /path/to/voicemode-local/.venv/bin/python -m voicemode_sysaudio.mcp_server

Then, mid voice conversation, the user can say things like "fold in the Teams
audio" / "stop including the system audio", and the assistant calls system_audio.
"""
from mcp.server.fastmcp import FastMCP

from . import platform_kind, set_system_audio

mcp = FastMCP("voicemode-sysaudio")


@mcp.tool()
def system_audio(state: str = "status") -> str:
    """Fold the computer's system/output audio (e.g. a Teams colleague's voice)
    into voicemode's microphone input, or toggle it back off.

    Use ONLY when the user explicitly asks to include or exclude what they are
    hearing in the voice transcription (e.g. "include the Teams audio", "stop
    capturing system sound", "what's my system-audio status?").

    Args:
        state: one of "on", "off", "status", "setup", "teardown".
            on/off   – fold system output into the mic mix, or remove it
            status   – report whether it is currently folded in
            setup    – one-time wiring of the capture mix (platform-specific)
            teardown – remove the capture mix entirely
    """
    if state not in ("on", "off", "status", "setup", "teardown"):
        return (f"invalid state {state!r}; use on|off|status|setup|teardown")
    r = set_system_audio(state)
    tail = f" — {r.detail}" if r.detail else ""
    return f"system audio {r.state} (platform={r.platform}, ok={r.ok}){tail}"


def main():
    mcp.run()


if __name__ == "__main__":
    main()
