"""Deadlines for audio-device calls, and a watchdog for the voice server's event loop.

FELIX #2095 (2026-11-27, a Mac): converse started, the client gave up after 180 s, and even
``voice_stop`` (one unlink) timed out after 180 s, while the pill showed Speaking with no
audio. So the event loop answered nothing. upstream 8.12.0 makes several PortAudio calls
straight on the loop, and one of them waits with no timeout at all:

- the chimes and system audio (core.py) call ``player.play(..., blocking=True)``, which
  waits for the end of playback with no timeout. An output device that stops calling back
  never ends it.
- every converse starts with ``sd._terminate(); sd._initialize()`` on the loop.

Which call hung there is not known (no stack was taken). So this module does two things:

1. ``play`` and ``call`` run such a call on a thread of its own, with a deadline. A missed
   deadline marks the process ``wedged``: a CoreAudio call that does not return cannot be
   cancelled from Python, and PortAudio is in an unknown state after it. ``converse`` then
   ends with an error that says what to do, and the process exits shortly after, so the
   host starts a fresh one (or the user restarts the chat).
2. ``Watchdog``: a task on the loop stamps a heartbeat every second; a plain thread checks
   it. A loop stuck longer than VOICEMODE_LOOP_STALL_EXIT_S writes the stack of every
   thread to logs/stall/stall-<UTC>.txt (the evidence the field report lacked) and exits.
   ``faulthandler.dump_traceback_later`` is the backstop when the GIL itself is held.
   The control socket's status reply carries ``loop_lag_s`` from here, so the pill (a
   separate process) can show "not responding" instead of a stale Speaking.

Settings (process environment; voice-mode loads voicemode.env into it):
  VOICEMODE_AUDIO_DEADLINE_S    slack after the sound's own length (default 10; 0 = off,
                                calls run on the loop with no deadline, as before)
  VOICEMODE_LOOP_STALL_EXIT_S   loop stall before the process exits (default 90; 0 = off)
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import math
import os
import sys
import threading
import time

logger = logging.getLogger("voicemode.audio_deadline")

DEADLINE_SETTING = "VOICEMODE_AUDIO_DEADLINE_S"
DEADLINE_DEFAULT = 10.0
STALL_SETTING = "VOICEMODE_LOOP_STALL_EXIT_S"
STALL_DEFAULT = 90.0
EXIT_CODE = 75            # EX_TEMPFAIL: try again with a fresh process
EXIT_DELAY_S = 2.0        # lets the converse result reach the client before the exit
WEDGED_EXIT_CAP_S = 30.0  # wedged but the turn never returned (a wait with no deadline): exit anyway
REARM_S = 10.0            # the faulthandler backstop is re-armed this often, not every beat
BEAT_S = 1.0
BACKSTOP_EXTRA_S = 15.0   # faulthandler fires this long after the watchdog thread should have

WEDGED_MESSAGE = (
    "Voice stopped: the audio device did not respond ({reason}). The voice server exits now "
    "so a fresh one can start. Continue in text. If the next voice call fails, restart the "
    "chat (in FELIX, restart FELIX); unplugging or reconnecting headphones may have caused it.")

# Set once an audio-device call missed its deadline; the process is not trusted after it.
wedged: str | None = None
_exit_at: float | None = None      # monotonic time of the planned exit (the earliest asked for)
_exit_lock = threading.Lock()


class DeadlineMissed(Exception):
    """An audio-device call did not return in time. The thread running it is abandoned."""


def _seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        v = float(raw) if raw.strip() else default
    except ValueError:
        return default
    return v if math.isfinite(v) and v >= 0 else default


def deadline_slack() -> float:
    return _seconds(DEADLINE_SETTING, DEADLINE_DEFAULT)


def stall_limit() -> float:
    return _seconds(STALL_SETTING, STALL_DEFAULT)


def _exit_now(code: int = EXIT_CODE) -> None:  # replaced in tests
    # No logging.shutdown(): it takes every handler's lock, and a thread blocked on a full
    # stderr pipe would hold one for ever (code review of 33d87cb). The stall file is on disk.
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def mark_wedged(reason: str) -> None:
    global wedged
    if wedged is None:
        wedged = reason
        logger.error("audio device not responding: %s; the voice server will exit after this turn", reason)
        dump_stacks(f"audio deadline missed: {reason}")
        # The turn may never return (upstream swallows the miss, then waits with no deadline):
        # exit anyway after a while. A reply that does go out brings the exit forward.
        schedule_exit(WEDGED_EXIT_CAP_S)


def schedule_exit(delay: float | None = None) -> None:
    """Exit after ``delay`` s (default EXIT_DELAY_S, read now) from a daemon thread, so a
    reply already returned is sent. A thread, not the loop: the loop may be what is stuck.
    Called again with a shorter delay, the exit comes earlier; never later."""
    global _exit_at
    wait = EXIT_DELAY_S if delay is None else delay
    with _exit_lock:
        first = _exit_at is None
        at = time.monotonic() + wait
        if first or at < _exit_at:
            _exit_at = at
    if not first:
        return

    def later():
        while time.monotonic() < _exit_at:
            time.sleep(min(0.1, max(0.0, _exit_at - time.monotonic())))
        _exit_now()

    threading.Thread(target=later, name="vml-audio-exit", daemon=True).start()


def wedged_result() -> str | None:
    """The converse result while the process is wedged (and the exit, scheduled once)."""
    if wedged is None:
        return None
    schedule_exit()
    return WEDGED_MESSAGE.format(reason=wedged)


async def call(fn, limit: float, what: str):
    """Run ``fn()`` on a daemon thread of its own and wait at most ``limit`` s.

    Not the default executor: an abandoned thread there is joined when asyncio shuts down,
    which is exactly the exit that must not wait (upstream #554/#555). ``limit`` <= 0 runs
    ``fn`` on the loop, as before. A miss marks the process wedged and raises DeadlineMissed.
    """
    if wedged is not None:  # the device already failed once: do not wait on it again
        raise DeadlineMissed(wedged)
    if limit <= 0:
        return fn()
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def settle(outcome):
        if not fut.done():
            fut.set_result(outcome)

    def run():
        try:
            outcome = (True, fn())
        except BaseException as e:  # handed to the awaiting side
            outcome = (False, e)
        try:
            loop.call_soon_threadsafe(settle, outcome)
        except RuntimeError:  # the loop is gone: nobody is waiting any more
            pass

    threading.Thread(target=run, name=f"vml-audio-{what}", daemon=True).start()
    try:
        ok, value = await asyncio.wait_for(asyncio.shield(fut), limit)
    except asyncio.TimeoutError:
        reason = f"{what} did not return within {limit:g} s"
        mark_wedged(reason)
        raise DeadlineMissed(reason) from None
    if not ok:
        raise value
    return value


async def play(player, samples, sample_rate) -> None:
    """``player.play(samples, sample_rate, blocking=True)`` with a deadline, off the loop.

    The deadline is the sound's length plus VOICEMODE_AUDIO_DEADLINE_S. A device that never
    finished the sound (no end-of-stream callback) also counts as a miss, even if closing
    the stream then returned."""
    if wedged is not None:
        raise DeadlineMissed(wedged)
    slack = deadline_slack()
    if slack <= 0:
        player.play(samples, sample_rate, blocking=True)
        return
    length = len(samples) / float(sample_rate)

    def run():
        player.play(samples, sample_rate, blocking=False)
        player.wait(timeout=length + slack)  # upstream wait() also stops and closes the stream
        return player.playback_complete.is_set()

    finished = await call(run, length + 2 * slack, "playback")
    if not finished:
        reason = f"playback did not finish within {length + slack:g} s"
        mark_wedged(reason)
        raise DeadlineMissed(reason)


KEEP_DUMPS = 5


def stall_dir():
    """<VOICEMODE_BASE_DIR, else ~/.voicemode>/logs/stall: voice-mode logs only to stderr
    unless VOICEMODE_DEBUG is set, and a host (Copilot desktop) may not keep stderr."""
    from pathlib import Path
    base = os.environ.get("VOICEMODE_BASE_DIR") or str(Path.home() / ".voicemode")
    return Path(os.path.expanduser(base)) / "logs" / "stall"


def _stack_text(why: str, lag) -> str:
    """Reason, loop lag and every thread's stack. Stacks only: no locals, arguments or
    message text, so the file can be attached to a report that leaves the machine."""
    import traceback
    lines = [f"reason: {why}",
             f"time (UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
             f"voice-mode pid: {os.getpid()}",
             f"loop_lag_s: {lag if lag is not None else loop_lag()}",
             f"python: {sys.version.split()[0]} on {sys.platform}", ""]
    names = {t.ident: t.name for t in threading.enumerate()}
    for ident, frame in sys._current_frames().items():
        lines.append(f"thread {names.get(ident, '?')} ({ident}):")
        lines.append("".join(traceback.format_stack(frame)))
    return "\n".join(lines)


def _prune(folder, pattern: str, keep: int) -> None:
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)
    for old in files[:max(0, len(files) - keep)]:
        try:
            old.unlink()
        except OSError:
            pass


def write_dump(why: str, lag=None):
    """logs/stall/stall-<UTC yyyymmddThhmmssZ>.txt (a -N suffix if that second is taken);
    the last KEEP_DUMPS are kept. Returns the path, or None if it could not be written."""
    try:
        folder = stall_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path, n = folder / f"stall-{stamp}.txt", 1
        while path.exists():
            path, n = folder / f"stall-{stamp}-{n}.txt", n + 1
        path.write_text(_stack_text(why, lag), encoding="utf-8")
        _prune(folder, "stall-*.txt", KEEP_DUMPS)
        return path
    except Exception:
        return None


def _keep_as_live(raw) -> bool:
    """Skip this file as a running server's? POSIX asks _pid_alive. Windows cannot ask (see
    there), but it refuses to delete a file another process holds open, so the unlink below
    is tried and a live server's file survives it (the OSError is ignored)."""
    return os.name != "nt" and _pid_alive(raw)


def _pid_alive(raw) -> bool:
    """Is that voice server still running? True when unsure, so its file is kept. On Windows
    os.kill(pid, 0) would TERMINATE the process, so it is never called there; an open file
    cannot be deleted on Windows anyway."""
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def open_backstop():
    """Open logs/stall/backstop-<pid>.txt in advance for faulthandler, which can only write
    to an open file. Empty backstop files of earlier runs are removed (a non-empty one
    fired: kept, the last KEEP_DUMPS of them). None if it cannot be opened."""
    try:
        folder = stall_dir()
        folder.mkdir(parents=True, exist_ok=True)
        mine = folder / f"backstop-{os.getpid()}.txt"
        fired = []   # files of servers no longer running that hold a dump
        for old in folder.glob("backstop-*.txt"):
            if old == mine or _keep_as_live(old.stem.split("-", 1)[1]):
                continue   # one per voice server: never another live server's file
            try:
                if old.stat().st_size == 0:
                    old.unlink()
                else:
                    fired.append((old.stat().st_mtime, old))
            except OSError:
                pass
        # Keep the last KEEP_DUMPS that fired. Live servers' files do not count and are never
        # pruned (Talos on FELIX #2104: a seventh server unlinked the oldest live one's file).
        for _, old in sorted(fired)[:max(0, len(fired) - KEEP_DUMPS)]:
            try:
                old.unlink()
            except OSError:
                pass
        return open(mine, "w", encoding="utf-8")
    except Exception:
        return None


GUARD_POLL_S = 0.25


async def guard(coro):
    """Run one converse turn; end it as soon as a device call inside misses its deadline.

    upstream catches the miss (``except Exception`` around the chimes, the cached replay)
    and carries on, and its TTS playback waits on ``asyncio.to_thread`` with no deadline,
    so the turn can hang with a healthy loop (code review of 33d87cb, finding 1). The turn
    is cancelled (a thread stuck inside is abandoned) and the wedged message returned."""
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=GUARD_POLL_S)
            if wedged is not None and not task.done():
                task.cancel()
                return wedged_result()
        try:
            result = task.result()
        except DeadlineMissed:
            return wedged_result()
    except asyncio.CancelledError:
        task.cancel()   # the caller was cancelled: take the turn down with it
        raise
    note = wedged_result()
    if note and isinstance(result, str):
        return result + "\n\n" + note
    return note if note and result is None else result


def dump_stacks(why: str, lag=None) -> None:
    """Every thread's stack: to a file of its own (stall-*.txt), to the log, and to stderr.
    The evidence for a hang; never allowed to cost the exit."""
    path = write_dump(why, lag)
    try:
        logger.error("voice server stack dump (%s) written to %s", why, path or "(could not write)")
        logger.error("%s", _stack_text(why, lag))
    except Exception:
        pass
    try:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception:
        pass


class Watchdog:
    """Exits the process when its event loop stops turning for longer than ``limit`` s.

    The loop stamps ``beat`` (monotonic) every second. A plain thread checks it; if that
    thread itself was away far longer than its interval, the machine slept (or the process
    was stopped), so the stamp is renewed instead of exiting: on Windows the monotonic clock
    runs on through sleep (GetTickCount64), on macOS and Linux it does not.
    """

    def __init__(self, limit: float, clock=time.monotonic, exit_fn=None, beat_s: float = BEAT_S):
        self.limit = limit
        self.clock = clock
        self.beat_s = beat_s
        self.beat = clock()
        self.exit_fn = exit_fn or (lambda: _exit_now())
        self._last_check = None
        self.fired = False
        self.backstop = None   # an open file for faulthandler (open_backstop), else stderr
        self._armed_at = None  # when the backstop was last re-armed (every REARM_S)

    def lag(self) -> float:
        return max(0.0, self.clock() - self.beat)

    def stamp(self) -> None:
        self.beat = self.clock()
        if self.limit > 0 and (self._armed_at is None or self.beat - self._armed_at >= REARM_S):
            self._armed_at = self.beat
            try:  # the backstop for a stall that holds the GIL, where no thread runs
                if self.backstop is not None:
                    faulthandler.dump_traceback_later(self.limit + BACKSTOP_EXTRA_S, exit=True,
                                                      file=self.backstop)
                else:
                    faulthandler.dump_traceback_later(self.limit + BACKSTOP_EXTRA_S, exit=True)
            except Exception:
                pass

    def check(self) -> bool:
        """One check (the thread calls it every beat). True when it fired."""
        now = self.clock()
        if self._last_check is not None and now - self._last_check > max(5 * self.beat_s, 5.0):
            self.beat = now  # we were not running either: sleep or SIGSTOP, not a stall
        self._last_check = now
        if self.fired or self.limit <= 0 or now - self.beat <= self.limit:
            return False
        self.fired = True
        reason = f"the voice server's event loop was stuck for {now - self.beat:.0f} s"
        logger.error("%s (limit %s=%g); exiting so a fresh one can start",
                     reason, STALL_SETTING, self.limit)
        dump_stacks(reason, lag=round(now - self.beat, 1))
        self.exit_fn()
        return True


_watchdog: Watchdog | None = None


def loop_lag() -> float | None:
    """Seconds since the event loop last turned, or None before the watchdog started."""
    return None if _watchdog is None else round(_watchdog.lag(), 1)


def ensure_started() -> None:
    """Start the heartbeat on the running loop and the checking thread, once per process.
    With VOICEMODE_LOOP_STALL_EXIT_S=0 the heartbeat still runs (the pill reads loop_lag)
    but nothing exits."""
    global _watchdog
    if _watchdog is not None:
        return
    loop = asyncio.get_running_loop()
    dog = Watchdog(stall_limit())
    if dog.limit > 0:
        dog.backstop = open_backstop()
    _watchdog = dog

    async def heartbeat():
        while True:
            dog.stamp()
            await asyncio.sleep(dog.beat_s)

    task = loop.create_task(heartbeat())
    _watchdog_task.append(task)  # keep a reference: the loop holds tasks only weakly

    def watch():
        while True:
            time.sleep(dog.beat_s)
            if dog.check():
                return

    threading.Thread(target=watch, name="vml-loop-watchdog", daemon=True).start()


_watchdog_task: list = []
