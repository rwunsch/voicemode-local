"""Fair holds for the conch (voicemode-local, 2026-11-11).

Upstream's hold_conch re-stamps a 10 s hold on every held turn, with no limit and no
look at the queue, so one session can keep another waiting for as long as it talks.
While someone else waits, a hold now stands only for a real back-and-forth:

- a held turn that got no reply releases the mic fully;
- at most VOICEMODE_CONCH_HOLD_MAX_TURNS held turns in a row (default 3) count from
  the moment someone started waiting; the next one releases.

With nobody waiting, a hold behaves exactly as upstream. Never raises.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger("voicemode.conch_hold")

MAX_TURNS_SETTING = "VOICEMODE_CONCH_HOLD_MAX_TURNS"
DEFAULT_MAX_TURNS = 3

_held: dict = {}  # session -> held turns in a row while someone waited


def reset() -> None:
    _held.clear()


def max_turns() -> int:
    try:
        value = int(os.environ.get(MAX_TURNS_SETTING, DEFAULT_MAX_TURNS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TURNS
    return value if value >= 0 else DEFAULT_MAX_TURNS


def is_reply(text) -> bool:
    """A transcript counts as a reply only if it has a letter or digit: whisper turns
    silence into dots (live 2026-11-11 21:29, ".\\n.\\n.\\n.\\n.")."""
    return isinstance(text, str) and any(ch.isalnum() for ch in text)


def others_waiting(session_id) -> bool:
    """Is anyone but this session in the conch queue? False when it cannot be read."""
    if __package__ != "voice_mode":
        return False
    try:
        from voice_mode.conch_queue import ConchQueue
        return any(e.session_id != session_id for e in ConchQueue.list())
    except Exception:  # noqa: BLE001 - unknown means upstream behaviour
        logger.debug("conch queue unreadable", exc_info=True)
        return False


def effective_hold(hold, session_id, replied: bool, waiting: Optional[bool] = None) -> bool:
    """The hold to release with: the caller's, unless it would starve a waiting session."""
    try:
        key = str(session_id)
        if not hold:
            _held.pop(key, None)
            return False
        if waiting is None:
            waiting = others_waiting(session_id)
        if not waiting:
            _held.pop(key, None)
            return True
        if not replied:
            _held.pop(key, None)
            logger.info("conch: hold released, no reply this turn and someone is waiting")
            return False
        count = _held.get(key, 0) + 1
        if count > max_turns():
            _held.pop(key, None)
            logger.info("conch: hold released after %d held turns, someone is waiting", max_turns())
            return False
        _held[key] = count
        return True
    except Exception:  # noqa: BLE001 - never break a release
        return bool(hold)
