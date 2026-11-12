# patches/ptt_listener_macos.py
"""Native macOS push-to-talk key listener.

Captures Control-Option-Space globally via pynput (Quartz event tap) and
forwards it to voice-mode's control channel, exactly like ptt_listener_linux:
key down sends ``hold_start`` at once (reply cut, mic open); key up after
``ptt_core.TAP_MAX_S`` (0.8 s) sends ``hold_release`` (transcribe and answer),
sooner it sends ``short_press``, which the control client turns into a full
``stop`` (back to text mode). The rule is the same on every platform.

Differences from the Linux listener:

- **Hotkey is Control-Option-Space**, not Control-Space. On macOS Control-Space
  is the system "select previous input source" shortcut, and Option-Space is
  taken by Handy (dictation). Override with ``VOICEMODE_PTT_MACOS_MODIFIERS``
  (comma list of ``ctrl``, ``alt``, ``cmd``, ``shift``; default ``ctrl,alt``).
- **No focus check.** There is no xdotool equivalent without extra deps, and the
  control socket only exists while a voice-mode server is up, so a stray press
  elsewhere is inert. Both hotkeys are swallowed (``darwin_intercept``) so they
  never reach the focused app; Escape would otherwise interrupt Claude Code.
- **Control-Option-Escape is a full stop**: cuts the reply and does not listen
  (control command ``stop``). The converse call returns cleanly with a control
  marker, so the agent carries on in text. Same modifiers as the PTT key.
- **Wake from idle.** With no voice call running there is no control socket,
  so a Control-Option-Space HOLD (released after TAP_MAX_S) instead wakes the
  session you last spoke with; a tap while idle does nothing. The wake: it finds that session's voice-mode pid in the conch events log, maps it
  to its tty, selects the Terminal.app tab with that exact tty, types
  ``WAKE_TEXT`` and presses Return. ``do script`` alone cannot submit to
  Claude Code (its newline lands in the input box), so Return is a System
  Events key press, sent only once Control-Option are released (a held
  modifier turns it into Control-Option-Return, which does not submit).
  ``WAKE_TEXT`` must stay short: a long line is taken for a paste and the
  Return after it does not submit. Key auto-repeat is ignored. Selecting the tab briefly brings it to
  the front.
  Terminal.app only; does nothing if the tab cannot be found. Disable with
  ``VOICEMODE_PTT_WAKE=false``.
- **One listener for all sessions.** The control socket path is shared, so
  ``voicemode-mcp`` starts this once (pidfile-guarded) and every session uses it.

Permissions: the first run needs **Input Monitoring** (System Settings →
Privacy & Security) for the app that launched it, typically your terminal.
Without it the event tap receives nothing and PTT is silently inert.

Requires ``VOICEMODE_CONTROL_CHANNEL_ENABLED=true``.

Installed into voice_mode/ptt_listener_macos.py by patches/apply.sh.
Run standalone: `python3 -m voice_mode.ptt_listener_macos`
"""
import glob
import json
import logging
import os
import signal
import subprocess
import threading
import time

logger = logging.getLogger("voicemode.ptt_listener_macos")

DEFAULT_MODIFIERS = "ctrl,alt"

DEFAULT_RELEASE_TAIL = 0.35  # seconds of recording kept after the key comes up

KEYCODE_SPACE = 49
KEYCODE_ESC = 53


def touch_pill() -> None:
    """Tell the voice pill a key press happened (its two-minute rule). Never raises."""
    try:
        try:
            from voice_mode import voice_pill_state as st
        except ImportError:  # run from the repo, not installed
            import voice_pill_state as st
        st.touch_ptt()
    except Exception:
        logger.debug("pill touch failed", exc_info=True)


# Keep this short. A 127-char line never submitted: Claude Code took the burst
# for a paste and the Return after it did not send. ~50 chars works (2026-10-20).
# It must name a message: voice-mode 8.12's converse refuses a call with neither
# `message` nor `turns` (live Mac test, 2026-11-07); the message
# is spoken as the cue, then converse listens (a silent wake was heard live, 2026-11-07). 44 chars.
WAKE_TEXT = '[push-to-talk] converse message="Listening."'
WAKE_COOLDOWN = 5.0  # seconds


def last_voice_tty():
    """tty of the live voice-mode server that most recently held the conch.

    The conch events log records the holder pid on every acquire/release. Walk
    it newest-first and return the tty of the first pid that is still alive
    and attached to a terminal (stale, detached servers show tty "??").
    """
    files = sorted(glob.glob(os.path.expanduser(
        "~/.voicemode/logs/events/voicemode_events_*.jsonl")), reverse=True)
    for path in files[:2]:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"CONCH_' not in line:
                continue
            try:
                pid = json.loads(line)["data"]["pid"]
            except (ValueError, KeyError, TypeError):
                continue
            tty = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout.strip()
            if tty and tty != "??":
                return "/dev/" + tty
    return None


WAKE_SCRIPT = """
on run argv
  set targetTty to item 1 of argv
  set wakeText to item 2 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is targetTty then
          set selected of t to true
          set index of w to 1
          activate
          do script wakeText in t
          delay 0.4
          tell application "System Events" to key code 36
          return "woke " & targetTty
        end if
      end repeat
    end repeat
  end tell
  return "no Terminal tab for " & targetTty
end run
"""


def wait_modifiers_released(flags: int, timeout: float = 5.0) -> None:
    """Block until the hotkey's modifiers are physically up (or timeout).

    Return posted while Control-Option is still held arrives as
    Control-Option-Return, which Claude Code does not treat as submit, and
    clearing the event's flags does not help: macOS merges the live modifier
    state in (both tried 2026-10-20).
    """
    import Quartz

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)
        if not state & flags:
            return
        time.sleep(0.03)


def wake_last_session(flags: int = 0) -> None:
    wait_modifiers_released(flags)
    tty = last_voice_tty()
    if tty is None:
        logger.info("PTT wake: no live voice session with a terminal found")
        return
    out = subprocess.run(["osascript", "-e", WAKE_SCRIPT, tty, WAKE_TEXT],
                         capture_output=True, text=True, timeout=10)
    result = (out.stdout or out.stderr).strip()
    logger.info("PTT wake: %s", result)


class MacHotkey:
    """The space key of Control-Option-Space as a down/up pair. Pure: every effect is
    injected, so the rule is testable without a Mac.

    ``mods`` says whether the hotkey's modifiers are down at that moment. While a
    voice call serves the control socket (``available()``), down sends hold_start
    and up sends short_press or hold_release (the latter ``release_tail`` seconds
    late: people let go a beat before their last syllable ends; without a tail the
    final word was clipped, 2026-10-20). With no call running, a hold of at least
    the state's ``tap_max`` calls ``wake()`` on release; a tap does nothing.
    """

    def __init__(self, state, *, emit, available, wake, timer, touch,
                 release_tail=DEFAULT_RELEASE_TAIL, wake_enabled=True):
        self._state, self._emit, self._available = state, emit, available
        self._wake, self._timer, self._touch = wake, timer, touch
        self._tail, self._wake_enabled = release_tail, wake_enabled
        self._held = False
        self._pending = None   # a hold_release waiting out its tail
        self._idle_down = None      # when an idle press went down
        self._last_wake = None

    def space(self, down: bool, ts: float, mods: bool) -> None:
        if down:
            if self._held or self._idle_down is not None or not mods:
                return  # auto-repeat, or not our hotkey
            self._touch()
            if not self._available():
                self._idle_down = ts
                return
            self._held = True
            self._send(self._state.on_press(ts))
            return
        if self._idle_down is not None:
            held_for, self._idle_down = ts - self._idle_down, None
            if (self._wake_enabled and held_for >= self._state.tap_max - 1e-9
                    and (self._last_wake is None or ts - self._last_wake > WAKE_COOLDOWN)):
                self._last_wake = ts
                self._wake()
            return
        if not self._held:
            return
        self._held = False
        action = self._state.on_release(ts)
        if action is not None and action.value == "hold_release" and self._tail > 0:
            self._pending = action            # release_held sends it if we stop first
            self._timer(self._tail, self._send_pending)
        else:
            self._send(action)

    def _send_pending(self) -> None:
        action, self._pending = getattr(self, "_pending", None), None
        self._send(action)

    def release_held(self, ts: float) -> None:
        """The listener is stopping. If a hold is active, send hold_release at
        once (no tail: the process is exiting) so the server's hold is not left
        latched with the mic open. Otherwise nothing."""
        self._idle_down = None
        if getattr(self, "_pending", None) is not None:   # a release waiting out its tail
            self._send_pending()
            return
        if not self._held:
            return
        self._held = False
        self._state.on_release(ts)
        self._emit("hold_release")

    def _send(self, action) -> None:
        if action is not None:
            logger.info("PTT %s", action.value)
            self._emit(action.value)


def modifier_groups(spec: str, keyboard) -> list:
    """Map 'ctrl,alt' to [{ctrl, ctrl_l, ctrl_r}, {alt, alt_l, alt_r}]."""
    names = {
        "ctrl": ("ctrl", "ctrl_l", "ctrl_r"),
        "alt": ("alt", "alt_l", "alt_r"),
        "cmd": ("cmd", "cmd_l", "cmd_r"),
        "shift": ("shift", "shift_l", "shift_r"),
    }
    groups = []
    for part in (p.strip().lower() for p in spec.split(",")):
        if part in names:
            groups.append({getattr(keyboard.Key, n) for n in names[part]
                           if hasattr(keyboard.Key, n)})
    return groups


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def install_stop_signals() -> None:
    """SIGTERM and SIGHUP stop the listener the way Ctrl-C does, so its finally
    runs and an active hold is released. Main thread only; elsewhere a no-op."""
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _interrupt)
        except ValueError:  # not the main thread
            pass


def run_listener() -> None:
    """Capture the hotkey and drive the control channel until interrupted."""
    from pynput import keyboard

    # Deferred: import the voice_mode-installed modules here, not at module
    # level, so this file stays importable on a fresh checkout.
    from voice_mode import ptt_core, ptt_control_client

    if not ptt_control_client.available():
        logger.warning(
            "voice-mode control socket at %s is not accepting connections yet. "
            "PTT stays inert until a voice-mode server runs with "
            "VOICEMODE_CONTROL_CHANNEL_ENABLED=true.",
            ptt_control_client.socket_path(),
        )

    spec = os.getenv("VOICEMODE_PTT_MACOS_MODIFIERS", DEFAULT_MODIFIERS)
    groups = modifier_groups(spec, keyboard)
    all_mods = set().union(*groups) if groups else set()
    down = set()

    def _timer(delay, fn):
        threading.Timer(delay, fn).start()

    def _wake():
        threading.Thread(target=wake_last_session, args=(want_flags,), daemon=True).start()

    hotkey = MacHotkey(
        ptt_core.PTTKeyState(), emit=ptt_control_client.on_action,
        available=ptt_control_client.available, wake=_wake, timer=_timer, touch=touch_pill,
        release_tail=float(os.getenv("VOICEMODE_PTT_RELEASE_TAIL", str(DEFAULT_RELEASE_TAIL))),
        wake_enabled=os.getenv("VOICEMODE_PTT_WAKE", "true").lower() != "false")

    def on_press(key):
        if key in all_mods:
            down.add(key)
        elif key == keyboard.Key.space:
            hotkey.space(True, time.monotonic(), mods=all(down & g for g in groups))

    def on_release(key):
        if key in all_mods:
            down.discard(key)
        elif key == keyboard.Key.space:
            hotkey.space(False, time.monotonic(), mods=True)

    import Quartz

    want_flags = 0
    for part in (p.strip().lower() for p in spec.split(",")):
        want_flags |= {"ctrl": Quartz.kCGEventFlagMaskControl,
                       "alt": Quartz.kCGEventFlagMaskAlternate,
                       "cmd": Quartz.kCGEventFlagMaskCommand,
                       "shift": Quartz.kCGEventFlagMaskShift}.get(part, 0)

    def intercept(event_type, event):
        """Swallow our hotkeys so they never reach the focused app.

        Escape in particular must not reach Claude Code: there it interrupts
        the running tool call, tearing down converse instead of the clean
        control-channel stop. The stop key is detected here on the raw keycode,
        which does not depend on pynput's modifier tracking.
        """
        if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
            return event
        code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        if code not in (KEYCODE_SPACE, KEYCODE_ESC):
            return event
        if (Quartz.CGEventGetFlags(event) & want_flags) != want_flags:
            return event
        if code == KEYCODE_ESC and event_type == Quartz.kCGEventKeyDown:
            logger.info("PTT stop")
            ptt_control_client.send("stop")
        return None

    install_stop_signals()
    listener = keyboard.Listener(on_press=on_press, on_release=on_release,
                                 darwin_intercept=intercept)
    listener.start()
    logger.info("PTT listener active (%s+space, %s+esc = stop) -> %s", spec, spec, ptt_control_client.socket_path())

    try:
        listener.join()
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        hotkey.release_held(time.monotonic())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_listener()
