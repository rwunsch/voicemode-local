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
import time
from typing import Optional

logger = logging.getLogger("voicemode.conch_hold")

MAX_TURNS_SETTING = "VOICEMODE_CONCH_HOLD_MAX_TURNS"
DEFAULT_MAX_TURNS = 3

_held: dict = {}  # session -> held turns in a row while someone waited
_kept_at: dict = {}  # session -> when its last hold was kept (epoch s)


def reset() -> None:
    _held.clear()
    _kept_at.clear()


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


def blocked_waiters(entries, session_id) -> list:
    """Waiters that are blocked on the mic: wait mode, not this session. A callback
    session keeps working while queued, so a hold cannot starve it (final review
    2026-11-11: counting it cut every hold short and added ~10 s of dead air)."""
    return [e for e in entries
            if getattr(e, "mode", "wait") == "wait" and e.session_id != session_id]


def others_waiting(session_id) -> bool:
    """Is another session blocked waiting for the mic? False when it cannot be read."""
    if __package__ != "voice_mode":
        return False
    try:
        from voice_mode.conch_queue import ConchQueue
        return bool(blocked_waiters(ConchQueue.list(), session_id))
    except Exception:  # noqa: BLE001 - unknown means upstream behaviour
        logger.debug("conch queue unreadable", exc_info=True)
        return False


def effective_hold(hold, session_id, replied: bool, waiting: Optional[bool] = None,
                   acquired_at: Optional[float] = None, expiry_s: float = 10.0,
                   now: Optional[float] = None) -> bool:
    """The hold to release with: the caller's, unless it would starve a waiting session.
    acquired_at (epoch seconds) is when this turn took the mic: later than the last kept
    hold plus its expiry means that hold lapsed, so the count starts again."""
    try:
        key = str(session_id)
        now = time.time() if now is None else now
        kept = _kept_at.pop(key, None)
        if kept is not None and acquired_at is not None and acquired_at > kept + expiry_s:
            _held.pop(key, None)
        if not hold:
            _held.pop(key, None)
            return False
        if waiting is None:
            waiting = others_waiting(session_id)
        if not waiting:
            _held.pop(key, None)
            _kept_at[key] = now
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
        _kept_at[key] = now
        return True
    except Exception:  # noqa: BLE001 - never break a release
        return bool(hold)
