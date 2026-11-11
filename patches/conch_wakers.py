"""Wake a queued voice session when the conch is granted to it, per harness (stdlib only).

Upstream 8.12's conch queue serves a queued caller in one of two modes:

  wait      the converse call blocks until the floor is ours, then speaks. Needs nobody
            to wake it, so it works in every harness.
  callback  converse returns at once, and the session must be pushed later to call
            converse again (conch_notify.notify_granted). Upstream's only push types into
            a tmux pane through the skillbox `session` binary; elsewhere it is a silent
            no-op, and callback grants never expire (conch_queue._grant_wedged), so an
            unwoken callback session blocks every other session. Seen in FELIX on Windows
            2026-11-11 (n=1): a Copilot chat queued with callback and the queue stood still.

Decision (Robert, 2026-11-11): one waker per harness behind upstream's push, and where
no waker can reach the session, callback is served as wait. In order:

  command      VOICEMODE_CONCH_WAKE_COMMAND, from the ENVIRONMENT only (a model can write
               voicemode.env): a JSON list of strings, or one executable path. It is run
               with the session id and the voice server pid appended; exit 0 = delivered.
               The seam for a harness's own waker, e.g. a Copilot app extension.
  claude-code  session_wake.py: Claude Code's own session socket, to the Claude Code
               process that started the queued voice server. POSIX only: on native
               Windows that path is unverified (see session_wake), so it does not count.
               Off with VOICEMODE_SESSION_WAKE=off.
  tmux         upstream's own `session send`, when that binary is on PATH.

converse (patch_conch_wakers.py) asks waker_for() when a callback is requested, and
conch_notify asks deliver() before upstream's own routes. Never raises.

Installed into voice_mode/conch_wakers.py by patches/apply.py.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("voicemode.conch_wakers")

COMMAND_SETTING = "VOICEMODE_CONCH_WAKE_COMMAND"
COMMAND_TIMEOUT_S = 10.0
CREATE_NO_WINDOW = 0x08000000
# Robert, 2026-11-11: a woken session gets 10 s to come back before present callers may
# pass it, and is woken at most 5 times per queued turn; it may stay silent.
GRACE_SETTING = "VOICEMODE_CONCH_CALLBACK_GRACE"
MAX_WAKES_SETTING = "VOICEMODE_CONCH_MAX_WAKES"
DEFAULT_GRACE_S = 10.0
DEFAULT_MAX_WAKES = 5
EXIT_BUSY = 75   # EX_TEMPFAIL: a harness waker reached the session, but it is busy
WAKES_FILE = "conch.wakes.json"


def grace_s() -> float:
    try:
        value = float(os.environ.get(GRACE_SETTING, DEFAULT_GRACE_S))
    except ValueError:
        return DEFAULT_GRACE_S
    return value if value >= 0 else DEFAULT_GRACE_S


def max_wakes() -> int:
    try:
        value = int(os.environ.get(MAX_WAKES_SETTING, DEFAULT_MAX_WAKES))
    except ValueError:
        return DEFAULT_MAX_WAKES
    return value if value >= 0 else DEFAULT_MAX_WAKES


def _age_s(grant: dict, now: Optional[datetime]) -> Optional[float]:
    try:
        at = datetime.fromisoformat(str(grant.get("granted_at")))
    except (TypeError, ValueError):
        return None
    now = now or (datetime.now(at.tzinfo) if at.tzinfo is not None else datetime.now())
    return (now - at).total_seconds()


def grant_is_absent(grant: dict, entry, now: Optional[datetime] = None, ttl: float = 30.0) -> bool:
    """A grant on a callback waiter that has not come back: past the grace, and for a
    `conch give` past the claim TTL too. Without a granted_at it is never absent."""
    try:
        if getattr(entry, "mode", "wait") != "callback":
            return False
        age = _age_s(grant, now)
        if age is None or age <= grace_s():
            return False
        return not grant.get("given") or age > ttl
    except Exception:  # noqa: BLE001 - unknown means "not absent": upstream behaviour
        return False


def take_wake(state_dir, session_id, seq) -> bool:
    """Count one wake for this queued turn; True while the count is within max_wakes()."""
    path = Path(state_dir) / WAKES_FILE
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    key = str(session_id)
    rec = data.get(key) if isinstance(data.get(key), dict) else {}
    count = (rec.get("count", 0) if rec.get("seq") == seq else 0) + 1
    data[key] = {"seq": seq, "count": count}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wakes-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        logger.debug("conch wake count not saved", exc_info=True)
    return count <= max_wakes()


def _session_wake():
    if __package__ == "voice_mode":
        from voice_mode import session_wake
    else:  # loaded from patches/ (tests): the sibling file
        import session_wake
    return session_wake


def wake_command() -> Optional[list]:
    """The harness's wake command from the environment, or None (also when malformed)."""
    raw = os.environ.get(COMMAND_SETTING, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list) and value and all(isinstance(a, str) and a for a in value):
        return value
    logger.warning("%s is not a JSON list of strings or one path; ignored", COMMAND_SETTING)
    return None


def claude_pid(voice_pid) -> Optional[int]:
    """The Claude Code process to wake for this voice server, where session_wake can."""
    if os.name == "nt" or not isinstance(voice_pid, int):
        return None
    sw = _session_wake()
    if not sw.enabled():
        return None
    return sw.claude_pid_for(voice_pid)


def tmux_available() -> bool:
    return shutil.which("session") is not None


def waker_for(voice_pid) -> Optional[str]:
    """Which waker could reach the session behind this voice server, or None."""
    try:
        if wake_command():
            return "command"
        if claude_pid(voice_pid):
            return "claude-code"
        if tmux_available():
            return "tmux"
    except Exception:  # noqa: BLE001 - an unknown answer means "no waker": wait is safe
        logger.debug("conch waker lookup failed", exc_info=True)
    return None


def _run_command(command, session_id, voice_pid) -> Optional[int]:
    kw = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        done = subprocess.run(command + [str(session_id or ""), str(voice_pid or "")],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=COMMAND_TIMEOUT_S, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("conch wake command failed: %s", type(e).__name__)
        return None
    if done.returncode not in (0, EXIT_BUSY):
        logger.warning("conch wake command exited %s", done.returncode)
    return done.returncode


def deliver(entry, state_dir=None) -> bool:
    """Wake a callback grantee with our wakers. True if one delivered; False leaves it
    to upstream's own route (tmux). With state_dir, at most max_wakes() per queued turn.
    Never raises."""
    try:
        pid = getattr(entry, "pid", None)
        session_id = getattr(entry, "session_id", None)
        if state_dir is not None and not take_wake(state_dir, session_id, getattr(entry, "seq", None)):
            logger.info("conch: session %s was woken %d times for this turn; not again",
                        session_id, max_wakes())
            return False
        command = wake_command()
        code = _run_command(command, session_id, pid) if command else None
        if code == 0:
            logger.info("conch: woke session %s through the harness command", session_id)
            return True
        if code == EXIT_BUSY:
            logger.info("conch: session %s is busy (harness command); it keeps its place", session_id)
            return False
        target = claude_pid(pid)
        if target:
            ok, detail = _session_wake().wake(target, reason="conch")
            logger.info("conch: Claude Code wake for session %s: %s", session_id, detail)
            return bool(ok)
    except Exception:  # noqa: BLE001 - a push must never break a grant site
        logger.debug("conch wake failed", exc_info=True)
    return False


def deliver_then(entry, fallback, state_dir=None) -> None:
    """deliver(), and upstream's local route when ours did not reach a local grantee."""
    if not deliver(entry, state_dir) and getattr(entry, "pid", None) is not None:
        try:
            fallback(entry)
        except Exception:  # noqa: BLE001
            pass
