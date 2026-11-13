"""MCP tools any client can call: voice_barge, voice_stop and voice_pill.

voice-mode's update_config can write the barge setting but cannot start or stop
the trigger process, so voice_barge goes through voicemode-switch.

Installed into voice_mode/tools/voice_barge_tool.py by patches/apply.sh.
"""
import asyncio
import os

from voice_mode import pill_autostart, voice_pill_state
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


PILL_ACTIONS = "show, hide or status"


def _pill_log() -> str:
    return str(voice_pill_state.default_base() / "autostart.log")


def _pill_show() -> str:
    if not voice_pill_state.pill_enabled():
        return ("The voice pill is switched off (VOICEMODE_PILL is not true), so it was not started. "
                "Switching it on is part of this install's voice setup.")
    voice_pill_state.set_pill_closed(False)
    try:
        started = pill_autostart.ensure()
        up = pill_autostart.backend_alive()
    except Exception as e:  # the tool answers in words, never with a traceback
        return f"Could not start the pill window ({type(e).__name__}: {e}); see {_pill_log()}."
    if started:
        return "The pill window is starting; it shows while a voice session is live."
    if up:
        return "The pill is already showing (its window is running); any earlier close is undone."
    return _pill_why_not()


def _pill_why_not() -> str:
    """Why ensure() started nothing, in the order pill_autostart.should_start/ensure check it,
    so a chat asked to open the pill can say why it did not."""
    from pathlib import Path
    kind = pill_autostart.platform_kind()
    if kind is None:
        return "There is no pill window on this system (it runs on Windows/WSL and macOS desktops)."
    if not pill_autostart.autostart_on():
        return ("The pill was not started: its autostart is switched off "
                "(VOICEMODE_PILL_AUTOSTART is off).")
    if kind == "macos" and not Path(pill_autostart.mac_python()).exists():
        return ("The Mac pill is not set up yet: its window environment is missing. "
                "Setting it up is part of this install's voice setup.")
    return (f"A pill start was already tried in the last {int(pill_autostart.LAUNCH_SPACING_S)} s; "
            f"it is probably still starting. If it does not appear, see {_pill_log()}.")


def _pill_hide() -> str:
    voice_pill_state.set_pill_closed(True)
    return "The pill is hidden; say 'show the pill' to bring it back."


def _pill_status() -> str:
    enabled = voice_pill_state.pill_enabled()
    closed = voice_pill_state.pill_closed()
    try:
        up = pill_autostart.backend_alive()
    except Exception:
        up = False
    return ("Pill is " + ("enabled" if enabled else "turned off (VOICEMODE_PILL is not true)")
            + "; " + ("its window is running" if up else "no window is running")
            + "; " + ("closed by the user (say 'show the pill' to bring it back)." if closed else "not closed."))


@mcp.tool()
async def voice_pill(action: str = "status") -> str:
    """Show, hide or check the voice pill: the small on-screen window with the voice status and controls.

    Use when the user asks to open, bring back, close or hide the pill.

    Args:
        action: "show", "hide" or "status". show also undoes the user's earlier close.
    """
    work = {"show": _pill_show, "hide": _pill_hide, "status": _pill_status}.get(
        action.strip().lower() if isinstance(action, str) else None)
    if work is None:
        return f"Unknown action {action!r}: use {PILL_ACTIONS}."
    try:
        return await asyncio.to_thread(work)
    except Exception as e:
        return f"The pill {action.strip().lower()} failed ({type(e).__name__}: {e})."
