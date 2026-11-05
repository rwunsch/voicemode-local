"""macOS window for the voice pill: ui/pill in a small frameless, always-on-top pywebview window.

Run by file path, with the interpreter of the pill venv (pywebview lives there, not in the
main venv):  ~/.voicemode/pill-venv/bin/python patches/voice_pill_mac.py [--force]

Runs patches/voice_pill.py as a child and speaks JSON lines over its stdin/stdout, as
pill-windows.ps1 does. Page -> wrapper: {"cmd":...} goes to the backend, {"window":...}
is handled here. The top half of this file is pure logic with no GUI import, so pytest
covers it on Linux. pywebview and pyobjc are imported lazily in main().

Never takes focus and has no Dock icon. pywebview asks for the Regular policy on import and
activates itself at start, so quiet_app() installs an application class that refuses both, before
pywebview loads. use_panel() swaps pywebview's window for a non-activating panel, so a click on
the pill leaves the frontmost app and its keyboard focus alone.
"""
import argparse
import fcntl
import json
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIDTH, MARGIN, DEFAULT_H = 350, 16, 68
MIN_WIDTH, MAX_WIDTH = 320, 640
TAB_W, TAB_H, RAIL_W = 22, 64, 76
BACKOFF_CAP_S = 30
NONACTIVATING_PANEL = 1 << 7  # NSWindowStyleMaskNonactivatingPanel
WINDOW_BEHAVIOUR = 1 << 0  # canJoinAllSpaces; Robert does not need it above full-screen apps
TRAY_SYMBOLS = {"speaking": "speaker.wave.2.fill", "listening": "mic.fill"}
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
    css_width: int = WIDTH
    dock_y: object = None  # centre of the docked window, top-left coordinates; None is the middle
    dismissed: bool = False
    visible: bool = False  # last snapshot's flag
    phase: str = ""

    @property
    def shown(self):
        return self.visible and not self.dismissed

    @property
    def in_tray(self):
        """Mode "dot" is minimized: the menu bar has it."""
        return self.shown and self.mode == "dot"

    @property
    def on_screen(self):
        return self.shown and self.mode != "dot"

    def on_window(self, w):
        """Apply a page {"window": ...} message. Returns 'drag', 'resize', 'changed' or None."""
        if "drag" in w:
            return "drag"
        if "resize" in w:
            return "resize"
        if "slide" in w:
            return "slide"
        if isinstance(w.get("phase"), str):
            self.phase = w["phase"]
            return "phase"
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

    Docked floats on the screen edge at its own height, as a slim "rail" or the full "docked"
    panel, and "tab" is what is left of it after sliding away. Both sit at dock_y, or the middle of the screen.
    """
    ax, ay, aw, ah = area
    if state.mode in ("docked", "rail", "tab"):
        w = {"tab": TAB_W, "rail": RAIL_W}.get(state.mode, state.css_width)
        h = TAB_H if state.mode == "tab" else state.css_height
        cy = ay + ah // 2 if state.dock_y is None else state.dock_y
        return (ax if state.side == "left" else ax + aw - w, max(ay, min(cy - h // 2, ay + ah - h)), w, h)
    w, h = state.css_width, state.css_height
    x, y = pos if pos else (ax + aw - w - MARGIN, ay + ah - DEFAULT_H - MARGIN)
    return (max(ax, min(x, ax + aw - w)), max(ay, min(y, ay + ah - h)), w, h)


def resized(edge, x, w, dx, area):
    """(x, w) after dragging an edge by dx. The opposite edge stays where it is."""
    ax, _, aw, _ = area
    room = x + w - ax if edge == "left" else ax + aw - x
    nw = max(MIN_WIDTH, min(MAX_WIDTH, room, w - dx if edge == "left" else w + dx))
    return (x + w - nw, nw) if edge == "left" else (x, nw)


def slide_off(side, area, width):
    """Where a slide ends, off the screen edge. AppKit refuses a fully off-screen move, so
    one point stays on screen."""
    return area[0] - width + 1 if side == "left" else area[0] + area[2] - 1


def slide_steps(start, end, n=10):
    """x positions for a slide, ending exactly on `end`."""
    return [start + (end - start) * i // n for i in range(1, n + 1)]


def parse_saved_position(text):
    """'x y [width [dock_y]]' -> ((x, y) or None, width or None, dock_y or None). '-' means not set."""
    p = text.split()
    if len(p) < 2 or len(p) > 4:
        return None, None, None
    try:
        pos = None if p[0] == "-" else (int(p[0]), int(p[1]))
        width = max(MIN_WIDTH, min(MAX_WIDTH, int(p[2]))) if len(p) > 2 and p[2] != "-" else None
        dock = int(p[3]) if len(p) > 3 and p[3] != "-" else None
    except ValueError:
        return None, None, None
    return pos, width, dock


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

# pywebview wraps evaluate_js and builds its js_api functions with eval()/new Function, which
# the page's strict CSP forbids. So: page -> Python through our own WKScriptMessageHandler
# ("pill"), Python -> page through WKWebView.evaluateJavaScript, and this script is a
# WKUserScript (WebKit exempts those from the page CSP).
START_OPTIONS = {"http_server": False}
HANDLER = "pill"
BRIDGE = """
window.pillTransport={send:function(j){window.webkit.messageHandlers.pill.postMessage(j)}};
document.addEventListener('DOMContentLoaded',function(){var c=document.getElementById('card');
new ResizeObserver(function(){if(c.offsetHeight)window.pillTransport.send(JSON.stringify({window:{height:c.offsetHeight}}))}).observe(c)});
"""


def ui_dir():
    return HERE.parent / "ui" / "pill"


def ui_file_url():
    # A plain path would make pywebview start a 127.0.0.1 HTTP server; file:// does not.
    return (ui_dir() / "index.html").as_uri()


def window_options():
    """create_window kwargs. No url: the wrapper loads the page itself, after the bridge is in."""
    return dict(title="Voice pill", width=WIDTH, height=DEFAULT_H, frameless=True, on_top=True,
                transparent=True, easy_drag=False, hidden=True)


def tray_symbol(phase):
    """The menu bar icon's SF Symbol. A template image, so it contrasts with any bar colour."""
    return TRAY_SYMBOLS.get(phase, "waveform")


def js_call(name, line):
    """One JS expression calling window.<name>(<line as a JSON string literal>)."""
    return f"window.{name}&&window.{name}({json.dumps(line)})"  # ensure_ascii: no raw U+2028


def run_on_main(fn, schedule=None, is_main=None):
    """Run fn on the AppKit main thread and return its result. AppKit aborts otherwise."""
    if is_main is None:
        from Foundation import NSThread
        is_main = NSThread.isMainThread
    if is_main():
        return fn()
    if schedule is None:
        from PyObjCTools import AppHelper
        schedule = AppHelper.callAfter
    done, box = threading.Event(), {}

    def go():
        try:
            box["r"] = fn()
        except BaseException as e:
            box["e"] = e
        done.set()
    schedule(go)
    if not done.wait(10):
        raise TimeoutError("main thread did not run the call")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def make_handler(app):
    from Foundation import NSObject

    class PillHandler(NSObject):
        def userContentController_didReceiveScriptMessage_(self, controller, message):
            app.from_page(str(message.body()))
    return PillHandler.alloc().init()


def make_tray_target(app):
    from Foundation import NSObject

    class PillTrayTarget(NSObject):
        def clicked_(self, sender):
            app.webview().evaluateJavaScript_completionHandler_("""window.pillReceive('{"cmd":"restore"}')""", None)
    return PillTrayTarget.alloc().init()


def quiet_app():
    """Before pywebview loads: no Dock icon, and the app can never activate itself."""
    from Foundation import NSBundle
    info = NSBundle.mainBundle().infoDictionary()
    if info is not None:  # first, before AppKit: this is what removes the Dock icon (measured)
        info["LSUIElement"] = "1"
    import objc
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    class PillApplication(NSApplication):
        def setActivationPolicy_(self, _policy):  # pywebview asks for Regular on import
            return objc.super(PillApplication, self).setActivationPolicy_(
                NSApplicationActivationPolicyAccessory)

        def activateIgnoringOtherApps_(self, _flag):  # pywebview activates at loop start
            pass
    PillApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)


def use_panel():
    """Before create_window: pywebview builds a non-activating panel that never becomes key."""
    import objc
    from AppKit import NSPanel
    from webview.platforms.cocoa import BrowserView

    class PillPanel(NSPanel):
        def initWithContentRect_styleMask_backing_defer_(self, rect, mask, backing, defer):
            return objc.super(PillPanel, self).initWithContentRect_styleMask_backing_defer_(
                rect, mask | NONACTIVATING_PANEL, backing, defer)

        def canBecomeKeyWindow(self):
            return False

    class WebKitHost(objc.Category(BrowserView.WebKitHost)):  # PyObjC requires this name
        def acceptsFirstMouse_(self, _event):  # the window is never key, so every click is a first
            return True
    BrowserView.WindowHost = PillPanel


class App:
    def __init__(self, force, backend):
        self.force = force
        self.backend = backend
        self.state = WindowState()
        self.pos = None
        self.pos_file = Path.home() / ".voicemode" / "pill" / "window-position.txt"
        self.lock = threading.Lock()
        self.proc = self.stdin = self.win = self.tray = self.tray_target = None
        self.slide_gen = 0
        self.sliding = False
        self.closing = self.had_output = self.dragging = False
        self.backoff = 1.0
        self.started = None
        try:
            self.pos, width, self.state.dock_y = parse_saved_position(self.pos_file.read_text())
            if width:
                self.state.css_width = width
        except OSError:
            pass

    def log(self, s):
        print(time.strftime("%H:%M:%S"), s, file=sys.stderr, flush=True)

    # backend
    def start_backend(self):
        if not self.closing:
            self.thread = threading.Thread(target=self.supervise, daemon=True)
            self.thread.start()

    def spawn(self):
        try:
            p = subprocess.Popen(self.backend, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8")
        except OSError as e:
            self.log(f"backend failed to start: {e}")
            return None
        self.proc, self.stdin = p, p.stdin
        self.log(f"backend started: pid {p.pid}")
        threading.Thread(target=lambda: [self.log("backend: " + l.rstrip()) for l in p.stderr], daemon=True).start()
        return p

    def supervise(self):
        """Run the backend, and run it again by the exit policy. One loop: no recursion."""
        env = Path.home() / ".voicemode" / "voicemode.env"
        while not self.closing:
            self.started = time.monotonic()
            p, lines = self.spawn(), 0
            if p:
                for line in p.stdout:
                    lines += 1
                    self.on_line(line.strip())
                p.wait()
            ran = time.monotonic() - self.started if p else 0  # a failed start ran for nothing
            code = p.returncode if p else 127
            self.stdin = self.proc = None
            if self.closing:
                return
            try:
                flag_on = self.force or pill_flag(env.read_text())
            except OSError:
                flag_on = True  # cannot tell: keep running
            d = on_backend_exit(code, lines, self.had_output, ran, self.backoff, flag_on)
            self.log(f"backend exited: code {code}, {lines} lines -> {d.action}")
            if d.action == "exit":
                self.exit_code = d.exit_code
                return self.shutdown()
            if d.action == "stay":
                return
            self.backoff = d.backoff
            time.sleep(d.delay)

    def on_line(self, line):
        self.had_output = True
        if self.closing or not self.win:
            return
        v = snapshot_visible(line)
        if v is not None and self.state.on_snapshot(v):
            self.apply()
        wv = self.webview()
        if wv is not None and self.page_ready:
            js = js_call("pillReceive", line)
            from PyObjCTools import AppHelper
            AppHelper.callAfter(wv.evaluateJavaScript_completionHandler_, js, None)  # not pywebview's eval wrapper

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
        prev = self.state.mode
        if "mode" in r[1]:
            self.slide_gen += 1  # a slide still running must not carry the window past the screen edge
        elif "height" in r[1] and self.sliding:
            self.state.on_window(r[1])  # the slide lands through apply() when it ends
            return
        what = self.state.on_window(r[1])
        if what == "drag":
            self.drag()
        elif what == "resize":
            self.resize(r[1]["resize"])
        elif what == "phase":
            self.update_tray()
        elif what == "slide":
            threading.Thread(target=self.slide, args=(True,), daemon=True).start()
        elif what == "changed":
            self.apply()
            if prev == "tab" and self.state.mode in ("docked", "rail") and self.state.shown:
                threading.Thread(target=self.slide, args=(False,), daemon=True).start()

    # window. Every AppKit call below runs on the main thread (run_on_main / callAfter).
    def webview(self):
        """The WKWebView, from pywebview's cocoa backend."""
        from webview.platforms.cocoa import BrowserView
        i = BrowserView.instances.get(self.win.uid) if self.win else None
        return i.webview if i else None

    def screen(self):
        from AppKit import NSScreen

        def read():
            s = NSScreen.screens()[0]  # the primary screen only
            f, v = s.frame(), s.visibleFrame()
            return top_left_area((f.origin.x, f.origin.y, f.size.width, f.size.height),
                                 (v.origin.x, v.origin.y, v.size.width, v.size.height))
        return run_on_main(read)

    def apply(self):
        from AppKit import NSMakeRect
        from PyObjCTools import AppHelper
        native = self.win.native if self.win else None
        if not native or self.closing:
            return
        self.update_tray()
        if not self.state.on_screen:
            return AppHelper.callAfter(native.orderOut_, None)

        def go():
            area, full_h = self.screen()  # inline: already on the main thread
            x, y, w, h = to_cocoa(geometry(self.state, area, self.pos), full_h)
            native.setFrame_display_(NSMakeRect(x, y, w, h), True)
            native.orderFrontRegardless()  # pywebview's own show() makes it key
        AppHelper.callAfter(go)

    def update_tray(self):
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self.update_tray_main)

    def update_tray_main(self):
        from AppKit import NSImage, NSStatusBar, NSVariableStatusItemLength
        want = self.state.in_tray
        if want and not self.tray:
            self.tray = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            self.tray_target = make_tray_target(self)
            self.tray.button().setTarget_(self.tray_target)
            self.tray.button().setAction_("clicked:")
        if not self.tray:
            return
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            tray_symbol(self.state.phase), "Voice pill")
        if image:
            image.setTemplate_(True)
            self.tray.button().setImage_(image)
        else:
            self.tray.button().setTitle_("\u25CF")
        self.tray.setVisible_(want)

    def install_bridge_and_load(self):
        """Main thread. Add the handler and the user script, then load the page from its file."""
        from Foundation import NSURL
        from WebKit import WKUserScript
        wv = self.webview()
        ucc = wv.configuration().userContentController()
        self.handler = make_handler(self)
        ucc.addScriptMessageHandler_name_(self.handler, HANDLER)
        ucc.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(BRIDGE, 0, True))
        wv.loadFileURL_allowingReadAccessToURL_(NSURL.URLWithString_(ui_file_url()),
                                                NSURL.fileURLWithPath_(str(ui_dir())))

    def drag(self):
        if self.dragging or self.state.mode == "tab":
            return
        self.dragging = True
        threading.Thread(target=self.drag_loop, daemon=True).start()

    def drag_loop(self):
        from AppKit import NSEvent, NSMakePoint
        from PyObjCTools import AppHelper
        try:
            native = self.win.native
            c0, o0 = NSEvent.mouseLocation(), run_on_main(lambda: native.frame().origin)
            moved = False
            vertical = self.state.mode in ("docked", "rail")  # docked: up and down the screen edge only
            while NSEvent.pressedMouseButtons() & 1:
                c = NSEvent.mouseLocation()
                x = o0.x if vertical else o0.x + c.x - c0.x
                AppHelper.callAfter(native.setFrameOrigin_, NSMakePoint(x, o0.y + c.y - c0.y))
                moved = moved or c.x != c0.x or c.y != c0.y
                time.sleep(0.016)
            if moved:
                time.sleep(0.05)  # let the last move land
                fr = run_on_main(lambda: native.frame())
                o, size = fr.origin, fr.size
                top = self.screen()[1] - o.y - size.height
                if vertical:
                    self.state.dock_y = int(top + size.height / 2)
                else:
                    self.pos = (int(o.x), int(top))
                self.save_position()
                self.apply()
        finally:
            self.dragging = False

    def save_position(self):
        try:
            pos = f"{self.pos[0]} {self.pos[1]}" if self.pos else "- -"
            dock = "-" if self.state.dock_y is None else self.state.dock_y
            self.pos_file.write_text(f"{pos} {self.state.css_width} {dock}")
        except OSError as e:
            self.log(f"position not saved: {e}")

    def slide(self, out):
        """Move the window off the screen edge, or back onto it from there."""
        from AppKit import NSMakePoint
        from PyObjCTools import AppHelper
        native = self.win.native
        gen = self.slide_gen
        self.sliding = True
        try:
            if not out:
                time.sleep(0.03)  # let apply() land
            if gen != self.slide_gen:
                return
            fr = run_on_main(lambda: native.frame())
            off = slide_off(self.state.side, self.screen()[0], int(fr.size.width))
            here = int(fr.origin.x)
            if not out:
                AppHelper.callAfter(native.setFrameOrigin_, NSMakePoint(off, fr.origin.y))
            for x in slide_steps(here, off) if out else slide_steps(off, here):
                if gen != self.slide_gen:
                    return
                AppHelper.callAfter(native.setFrameOrigin_, NSMakePoint(x, fr.origin.y))
                time.sleep(0.016)
        finally:
            self.sliding = False
        if not out:
            self.apply()  # the height may have changed on the way in

    def resize(self, edge):
        if self.dragging or self.state.mode in ("docked", "rail"):
            return
        self.dragging = True
        threading.Thread(target=self.resize_loop, args=(edge,), daemon=True).start()

    def resize_loop(self, edge):
        from AppKit import NSEvent
        try:
            area = self.screen()[0]
            x0, y0, w0, _ = geometry(self.state, area, self.pos)
            c0, last = NSEvent.mouseLocation().x, w0
            while NSEvent.pressedMouseButtons() & 1:
                x, w = resized(edge, x0, w0, NSEvent.mouseLocation().x - c0, area)
                if w != last:
                    last, self.pos, self.state.css_width = w, (int(x), int(y0)), int(w)
                    self.apply()
                time.sleep(0.016)
            if last != w0:
                self.save_position()
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
    thread = None
    handler = None
    page_ready = False
    stage = 0


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
    quiet_app()  # main thread, before pywebview's import can ask for Regular
    import webview
    from PyObjCTools import AppHelper

    use_panel()
    app = App(args.force, shlex.split(args.backend))
    app.win = webview.create_window(**window_options())

    def shown_on_main():
        try:  # all Spaces; best effort
            app.win.native.setCollectionBehavior_(WINDOW_BEHAVIOUR)
            app.win.native.setHidesOnDeactivate_(False)
        except Exception as e:
            app.log(f"window behaviour not set: {e}")

    def loaded_on_main():
        app.stage += 1
        if app.stage == 1:  # pywebview's blank page: now put the real one in
            app.install_bridge_and_load()
        else:
            app.page_ready = True
            if app.thread is None:
                app.start_backend()

    # These events fire on worker threads: hop to the main thread for anything AppKit.
    app.win.events.shown += lambda: AppHelper.callAfter(shown_on_main)
    app.win.events.loaded += lambda: AppHelper.callAfter(loaded_on_main)
    webview.start(**START_OPTIONS)
    app.shutdown()
    return app.exit_code


if __name__ == "__main__":
    sys.exit(main())
