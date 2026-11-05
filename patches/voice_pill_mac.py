"""macOS window for the voice pill: ui/pill in a small frameless, always-on-top pywebview window.

Run by file path, with the interpreter of the pill venv (pywebview lives there, not in the
main venv):  ~/.voicemode/pill-venv/bin/python patches/voice_pill_mac.py [--force]

Runs patches/voice_pill.py as a child and speaks JSON lines over its stdin/stdout, as
pill-windows.ps1 does. Page -> wrapper: {"cmd":...} goes to the backend, {"window":...}
is handled here. The top half of this file is pure logic with no GUI import, so pytest
covers it on Linux. pywebview and pyobjc are imported lazily in main().

Never takes focus and has no Dock icon: the activation policy is Accessory, set after
pywebview's import (which resets it to Regular) and before the loop starts.
"""
import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIDTH, DOT, MARGIN, DEFAULT_H = 320, 16, 16, 46
BACKOFF_CAP_S = 30
STABLE_RUN_S = 30


# -- pure logic ---------------------------------------------------------------

def route_page_message(text):
    """('cmd', text) for the backend, ('window', dict) for us, None for anything else."""
    try:
        m = json.loads(text)
    except ValueError:
        return None
    if not isinstance(m, dict):
        return None
    if "window" in m:
        return ("window", m["window"]) if isinstance(m["window"], dict) else None
    if "cmd" in m:
        return ("cmd", text)
    return None


def snapshot_visible(line):
    """The `visible` flag of a backend line, or None if it has none."""
    try:
        m = json.loads(line)
    except ValueError:
        return None
    v = m.get("visible") if isinstance(m, dict) else None
    return v if isinstance(v, bool) else None


@dataclass
class WindowState:
    mode: str = "pill"
    side: str = "right"
    css_height: int = DEFAULT_H
    dismissed: bool = False
    visible: bool = False  # last snapshot's flag

    @property
    def shown(self):
        return self.visible and not self.dismissed

    def on_window(self, w):
        """Apply a page {"window": ...} message. Returns 'drag', 'changed' or None."""
        if "drag" in w:
            return "drag"
        h = w.get("height")
        if isinstance(h, int) and not isinstance(h, bool):
            self.css_height = h
            return "changed"
        if isinstance(w.get("mode"), str):
            self.dismissed = w["mode"] == "hidden"
            if not self.dismissed:
                self.mode = w["mode"]
            self.side = "left" if w.get("side") == "left" else "right"
            return "changed"
        return None

    def on_snapshot(self, visible):
        changed = visible != self.visible
        self.visible = visible
        return changed


def geometry(state, area, pos=None):
    """(x, y, w, h) in points, y down. `area` is the usable screen (x, y, w, h).

    Docked means snapped to the screen edge at full height. macOS has no AppBar, so
    other windows are not pushed aside.
    """
    ax, ay, aw, ah = area
    if state.mode == "docked":
        return (ax if state.side == "left" else ax + aw - WIDTH, ay, WIDTH, ah)
    w, h = (DOT, DOT) if state.mode == "dot" else (WIDTH, state.css_height)
    x, y = pos if pos else (ax + aw - w - MARGIN, ay + ah - DEFAULT_H - MARGIN)
    return (max(ax, min(x, ax + aw - w)), max(ay, min(y, ay + ah - h)), w, h)


def top_left_area(frame, visible):
    """Cocoa (y up) screen frame + visible frame, each (x, y, w, h) -> (area, full_height)."""
    full_h = frame[3]
    return (visible[0], full_h - (visible[1] + visible[3]), visible[2], visible[3]), full_h


def to_cocoa(rect, full_h):
    x, y, w, h = rect
    return (x, full_h - y - h, w, h)


@dataclass
class Decision:
    action: str          # 'retry' | 'exit' | 'stay'
    exit_code: int = 0
    delay: float = 0.0
    backoff: float = 1.0


def on_backend_exit(code, lines, had_output, ran_s, backoff, flag_on):
    """What to do when the backend ends. Same policy as pill-windows.ps1."""
    if not flag_on:
        return Decision("exit")
    if code == 1 and lines == 0:
        # A second instance, or a failed import: retrying helps neither.
        return Decision("stay") if had_output else Decision("exit", 1)
    if ran_s >= STABLE_RUN_S:
        backoff = 1
    return Decision("retry", delay=backoff, backoff=min(backoff * 2, BACKOFF_CAP_S))


def pill_flag(env_text):
    """VOICEMODE_PILL from voicemode.env text: the last assignment wins."""
    val = None
    for line in env_text.splitlines():
        if line.startswith("VOICEMODE_PILL="):
            val = line.split("=", 1)[1].strip() == "true"
    return bool(val)


# -- the window (needs macOS; not covered by pytest) -------------------------

BRIDGE = """
window.pillTransport={send:function(j){if(window.pywebview&&window.pywebview.api)window.pywebview.api.send(j)}};
(function(){var c=document.getElementById('card');
function go(){new ResizeObserver(function(){if(c.offsetHeight)window.pillTransport.send(JSON.stringify({window:{height:c.offsetHeight}}))}).observe(c)}
if(window.pywebview&&window.pywebview.api)go();else window.addEventListener('pywebviewready',go)})();
"""


class Api:  # pywebview exposes public methods only
    def __init__(self, app):
        self._app = app

    def send(self, text):
        self._app.from_page(text)


class App:
    def __init__(self, force, backend):
        self.force = force
        self.backend = backend
        self.state = WindowState()
        self.pos = None
        self.pos_file = Path.home() / ".voicemode" / "pill" / "window-position.txt"
        self.lock = threading.Lock()
        self.proc = self.stdin = self.win = None
        self.closing = self.had_output = self.dragging = False
        self.backoff = 1.0
        self.started = None
        try:
            x, y = self.pos_file.read_text().split()
            self.pos = (int(x), int(y))
        except (OSError, ValueError):
            pass

    def log(self, s):
        print(time.strftime("%H:%M:%S"), s, file=sys.stderr, flush=True)

    # backend
    def start_backend(self):
        if self.closing:
            return
        cmd = self.backend
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8")
        except OSError as e:
            self.log(f"backend failed to start: {e}")
            return self.after_exit(None, 0)
        self.proc, self.stdin, self.started = p, p.stdin, time.monotonic()
        self.log(f"backend started: pid {p.pid}")
        threading.Thread(target=lambda: [self.log("backend: " + l.rstrip()) for l in p.stderr], daemon=True).start()
        threading.Thread(target=self.read_loop, args=(p,), daemon=True).start()

    def read_loop(self, p):
        lines = 0
        for line in p.stdout:
            lines += 1
            self.on_line(line.strip())
        p.wait()
        self.after_exit(p.returncode, lines)

    def after_exit(self, code, lines):
        self.stdin = self.proc = None
        if self.closing:
            return
        env = Path.home() / ".voicemode" / "voicemode.env"
        try:
            flag_on = self.force or pill_flag(env.read_text())
        except OSError:
            flag_on = True  # cannot tell: keep running
        ran = time.monotonic() - self.started if self.started else 0
        d = on_backend_exit(code if code is not None else 0, lines, self.had_output, ran, self.backoff, flag_on)
        self.log(f"backend exited: code {code}, {lines} lines -> {d.action}")
        if d.action == "exit":
            self.exit_code = d.exit_code
            return self.shutdown()
        if d.action == "retry":
            self.backoff = d.backoff
            time.sleep(d.delay)
            self.start_backend()

    def on_line(self, line):
        self.had_output = True
        if self.closing or not self.win:
            return
        v = snapshot_visible(line)
        if v is not None and self.state.on_snapshot(v):
            self.apply()
        try:
            self.win.evaluate_js("window.pillReceive&&window.pillReceive(" + json.dumps(line) + ")")
        except Exception as e:  # page not loaded yet, or closing
            self.log(f"page send failed: {e}")

    def from_page(self, text):
        r = route_page_message(text)
        if not r:
            return
        if r[0] == "cmd":
            try:
                if self.stdin:
                    self.stdin.write(r[1] + "\n")
                    self.stdin.flush()
            except (OSError, ValueError):
                pass
            return
        what = self.state.on_window(r[1])
        if what == "drag":
            self.drag()
        elif what == "changed":
            self.apply()

    # window
    def screen(self):
        from AppKit import NSScreen
        s = NSScreen.screens()[0]  # the primary screen only
        f, v = s.frame(), s.visibleFrame()
        return top_left_area((f.origin.x, f.origin.y, f.size.width, f.size.height),
                             (v.origin.x, v.origin.y, v.size.width, v.size.height))

    def apply(self):
        from AppKit import NSMakeRect
        from PyObjCTools import AppHelper
        native = self.win.native if self.win else None
        if not native or self.closing:
            return
        if not self.state.shown:
            return AppHelper.callAfter(native.orderOut_, None)
        area, full_h = self.screen()
        x, y, w, h = to_cocoa(geometry(self.state, area, self.pos), full_h)

        def go():
            native.setFrame_display_(NSMakeRect(x, y, w, h), True)
            native.orderFrontRegardless()  # pywebview's own show() activates the app
        AppHelper.callAfter(go)

    def drag(self):
        if self.dragging or self.state.mode == "docked":
            return
        self.dragging = True
        threading.Thread(target=self.drag_loop, daemon=True).start()

    def drag_loop(self):
        from AppKit import NSEvent, NSMakePoint
        from PyObjCTools import AppHelper
        try:
            native = self.win.native
            c0, o0 = NSEvent.mouseLocation(), native.frame().origin
            moved = False
            while NSEvent.pressedMouseButtons() & 1:
                c = NSEvent.mouseLocation()
                AppHelper.callAfter(native.setFrameOrigin_, NSMakePoint(o0.x + c.x - c0.x, o0.y + c.y - c0.y))
                moved = moved or c.x != c0.x or c.y != c0.y
                time.sleep(0.016)
            if moved:
                time.sleep(0.05)  # let the last move land
                o, size = native.frame().origin, native.frame().size
                self.pos = (int(o.x), int(self.screen()[1] - o.y - size.height))
                try:
                    self.pos_file.write_text(f"{self.pos[0]} {self.pos[1]}")
                except OSError as e:
                    self.log(f"position not saved: {e}")
                self.apply()
        finally:
            self.dragging = False

    def shutdown(self):
        if self.closing:
            return
        self.closing = True
        try:
            if self.stdin:
                self.stdin.close()  # EOF ends the backend and lifts its Pause
            if self.proc:
                self.proc.wait(2)
        except Exception:
            if self.proc:
                self.proc.kill()
        if self.win:
            try:
                self.win.destroy()
            except Exception:
                pass

    exit_code = 0


def single_instance():
    run = Path.home() / ".voicemode" / "run"
    run.mkdir(parents=True, exist_ok=True)
    f = open(run / "pill-window.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f  # held until exit


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run even if VOICEMODE_PILL is not true")
    ap.add_argument("--backend", default=f'"{sys.executable}" "{HERE / "voice_pill.py"}" --stdio',
                    help="backend command line (tests use tests/pill_fake_backend.py)")
    args = ap.parse_args(argv)
    if sys.platform != "darwin":
        print("voice_pill_mac: macOS only", file=sys.stderr)
        return 1
    env = Path.home() / ".voicemode" / "voicemode.env"
    try:
        if not args.force and not pill_flag(env.read_text()):
            print("voice_pill_mac: VOICEMODE_PILL is not true; exiting", file=sys.stderr)
            return 0
    except OSError:
        pass
    lock = single_instance()
    if lock is None:
        print("voice_pill_mac: another pill window is running; exiting", file=sys.stderr)
        return 0
    import webview  # imported here: it sets the activation policy to Regular on import
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSWorkspace

    def accessory():
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    accessory()
    prev = NSWorkspace.sharedWorkspace().frontmostApplication()
    app = App(args.force, shlex.split(args.backend))
    ui = HERE.parent / "ui" / "pill" / "index.html"
    app.win = webview.create_window("Voice pill", str(ui), width=WIDTH, height=DEFAULT_H, frameless=True,
                                    on_top=True, transparent=True, easy_drag=False, hidden=True,
                                    js_api=Api(app))

    def shown():
        # pywebview activates the app once when the loop starts: give focus back.
        accessory()
        if prev is not None and prev.processIdentifier() != os.getpid():
            prev.activateWithOptions_(0)
        try:  # all Spaces and above full-screen apps; best effort
            app.win.native.setCollectionBehavior_((1 << 0) | (1 << 8))
            app.win.native.setHidesOnDeactivate_(False)
        except Exception as e:
            app.log(f"window behaviour not set: {e}")

    def loaded():
        app.win.evaluate_js(BRIDGE)
        if app.proc is None and app.started is None:
            app.start_backend()

    app.win.events.shown += shown
    app.win.events.loaded += loaded
    webview.start()
    app.shutdown()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
