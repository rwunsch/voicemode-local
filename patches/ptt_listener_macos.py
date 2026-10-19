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
- **One listener for all sessions.** The control socket path is shared, so
  ``voicemode-mcp`` starts this once (pidfile-guarded) and every session uses it.

Permissions: the first run needs **Input Monitoring** (System Settings →
Privacy & Security) for the app that launched it, typically your terminal.
Without it the event tap receives nothing and PTT is silently inert.

Requires ``VOICEMODE_CONTROL_CHANNEL_ENABLED=true``.

Installed into voice_mode/ptt_listener_macos.py by patches/apply.sh.
Run standalone: `python3 -m voice_mode.ptt_listener_macos`
"""
import logging
import os
import time

logger = logging.getLogger("voicemode.ptt_listener_macos")

POLL_INTERVAL = 0.05  # seconds; how often to check the hold threshold while held

DEFAULT_MODIFIERS = "ctrl,alt"

KEYCODE_SPACE = 49
KEYCODE_ESC = 53


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
            held = True
            _emit(state.on_press(time.monotonic()))

    def on_release(key):
        nonlocal held
        if key in all_mods:
            down.discard(key)
            return
        if held and key == keyboard.Key.space:
            held = False
            _emit(state.on_release(time.monotonic()))

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
