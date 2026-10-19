# patches/ptt_listener_macos.py
"""Native macOS push-to-talk key listener.

Captures Control-Option-Space globally via pynput (Quartz event tap), classifies
press/hold/release with ``ptt_core.PTTKeyState``, and forwards the classified
action to voice-mode's control channel, exactly like ptt_listener_linux.

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
  so a Control-Option-Space press instead wakes the session you last spoke
  with: it finds that session's voice-mode pid in the conch events log, maps it
  to its tty, selects the Terminal.app tab with that exact tty, types
  ``WAKE_TEXT`` and presses Return. ``do script`` alone cannot submit to
  Claude Code (its newline lands in the input box), so Return is a real key
  press via System Events, which briefly brings that tab to the front.
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
import subprocess
import threading
import time

logger = logging.getLogger("voicemode.ptt_listener_macos")

POLL_INTERVAL = 0.05  # seconds; how often to check the hold threshold while held

DEFAULT_MODIFIERS = "ctrl,alt"

DEFAULT_RELEASE_TAIL = 0.35  # seconds of recording kept after the key comes up

KEYCODE_SPACE = 49
KEYCODE_ESC = 53


WAKE_TEXT = "[push-to-talk] resume the voice conversation"
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
          delay 0.2
          tell application "System Events" to key code 36
          return "woke " & targetTty
        end if
      end repeat
    end repeat
  end tell
  return "no Terminal tab for " & targetTty
end run
"""


def wake_last_session() -> None:
    tty = last_voice_tty()
    if tty is None:
        logger.info("PTT wake: no live voice session with a terminal found")
        return
    out = subprocess.run(["osascript", "-e", WAKE_SCRIPT, tty, WAKE_TEXT],
                         capture_output=True, text=True, timeout=10)
    logger.info("PTT wake: %s", (out.stdout or out.stderr).strip())


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
    state = ptt_core.PTTKeyState(
        hold_threshold=float(
            os.getenv("VOICEMODE_PTT_HOLD_THRESHOLD", str(ptt_core.DEFAULT_HOLD_THRESHOLD))
        )
    )
    last_wake = [0.0]
    wake_enabled = os.getenv("VOICEMODE_PTT_WAKE", "true").lower() != "false"
    release_tail = float(os.getenv("VOICEMODE_PTT_RELEASE_TAIL", str(DEFAULT_RELEASE_TAIL)))
    down = set()
    held = False

    def _emit(action) -> None:
        if action is None:
            return
        logger.info("PTT %s", action.value)
        ptt_control_client.on_action(action.value)

    def _mods_down() -> bool:
        return all(down & g for g in groups)

    def on_press(key):
        nonlocal held
        if key in all_mods:
            down.add(key)
            return
        if key == keyboard.Key.space and not held and _mods_down():
            if wake_enabled and not ptt_control_client.available():
                # Idle: no call to hold. Wake the last session instead. Key
                # auto-repeat re-fires on_press while held, hence the cooldown.
                now = time.monotonic()
                if now - last_wake[0] > WAKE_COOLDOWN:
                    last_wake[0] = now
                    threading.Thread(target=wake_last_session, daemon=True).start()
                return
            held = True
            _emit(state.on_press(time.monotonic()))

    def on_release(key):
        nonlocal held
        if key in all_mods:
            down.discard(key)
            return
        if held and key == keyboard.Key.space:
            held = False
            action = state.on_release(time.monotonic())
            if action is not None and action.value == "hold_release" and release_tail > 0:
                # People let go of the key a beat before their last syllable
                # ends; without a tail the final word was clipped (2026-10-20).
                threading.Timer(release_tail, _emit, args=(action,)).start()
            else:
                _emit(action)

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

    listener = keyboard.Listener(on_press=on_press, on_release=on_release,
                                 darwin_intercept=intercept)
    listener.start()
    logger.info("PTT listener active (%s+space, %s+esc = stop) -> %s", spec, spec, ptt_control_client.socket_path())

    try:
        while True:
            if held:
                _emit(state.poll_hold(time.monotonic()))
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_listener()
