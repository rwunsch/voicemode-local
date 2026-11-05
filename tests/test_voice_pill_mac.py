"""Mac pill wrapper: the logic that needs no GUI. pywebview is not imported here."""
import importlib.util
import sys
from pathlib import Path

PATCHES = Path(__file__).resolve().parent.parent / "patches"
spec = importlib.util.spec_from_file_location("voice_pill_mac", PATCHES / "voice_pill_mac.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

AREA = (0, 25, 1440, 800)  # below the menu bar


def test_imports_without_gui_libraries():
    assert "webview" not in sys.modules and "AppKit" not in sys.modules


def test_cmd_goes_to_backend_window_to_wrapper_rest_dropped():
    assert m.route_page_message('{"cmd":"pause"}') == ("cmd", '{"cmd":"pause"}')
    assert m.route_page_message('{"window":{"drag":true}}') == ("window", {"drag": True})
    for bad in ("nope", "[1]", '{"other":1}', '{"window":3}', "null"):
        assert m.route_page_message(bad) is None


def test_snapshot_visible_only_for_real_booleans():
    assert m.snapshot_visible('{"visible": true}') is True
    assert m.snapshot_visible('{"visible": false}') is False
    assert m.snapshot_visible('{"visible": 1}') is None
    assert m.snapshot_visible("garbage") is None


def test_window_messages_update_state():
    s = m.WindowState()
    assert s.on_window({"drag": True}) == "drag"
    assert s.on_window({"height": 120}) == "changed" and s.css_height == 120
    assert s.on_window({"height": True}) is None and s.css_height == 120
    assert s.on_window({"mode": "docked", "side": "left"}) == "changed"
    assert (s.mode, s.side, s.dismissed) == ("docked", "left", False)
    s.on_window({"mode": "hidden"})
    assert s.dismissed and s.mode == "docked"  # the mode to return to is kept
    s.on_window({"mode": "pill", "side": "bogus"})
    assert (s.mode, s.side, s.dismissed) == ("pill", "right", False)


def test_shown_needs_snapshot_visible_and_not_dismissed():
    s = m.WindowState()
    assert not s.shown
    assert s.on_snapshot(True) and s.shown
    assert not s.on_snapshot(True)
    s.on_window({"mode": "hidden"})
    assert not s.shown
    s.on_window({"mode": "pill"})
    s.on_snapshot(False)
    assert not s.shown


def test_geometry_per_mode():
    s = m.WindowState(visible=True)
    assert m.geometry(s, AREA) == (1440 - 320 - 16, 25 + 800 - 46 - 16, 320, 46)
    s.css_height = 200
    assert m.geometry(s, AREA, pos=(100, 100)) == (100, 100, 320, 200)
    s.mode = "dot"
    assert m.geometry(s, AREA, pos=(100, 100)) == (100, 100, 16, 16)
    s.mode, s.side = "docked", "right"
    assert m.geometry(s, AREA) == (1120, 25, 320, 800)
    s.side = "left"
    assert m.geometry(s, AREA) == (0, 25, 320, 800)


def test_saved_position_is_clamped_onto_the_screen():
    s = m.WindowState(visible=True, css_height=100)
    assert m.geometry(s, AREA, pos=(5000, -50)) == (1120, 25, 320, 100)
    assert m.geometry(s, AREA, pos=(-9, 5000)) == (0, 725, 320, 100)


def test_cocoa_coordinate_conversion():
    area, full_h = m.top_left_area((0, 0, 1440, 900), (0, 80, 1440, 795))  # Dock 80, menu bar 25
    assert (area, full_h) == ((0, 25, 1440, 795), 900)
    assert m.to_cocoa((10, 25, 320, 46), 900) == (10, 829, 320, 46)


def test_restart_backoff_doubles_to_the_cap_and_resets_after_a_stable_run():
    b, delays = 1, []
    for _ in range(7):
        d = m.on_backend_exit(2, 5, True, 1, b, True)
        assert d.action == "retry"
        delays.append(d.delay)
        b = d.backoff
    assert delays == [1, 2, 4, 8, 16, 30, 30]
    d = m.on_backend_exit(2, 5, True, 31, 16, True)
    assert d.delay == 1 and d.backoff == 2


def test_exit_policy():
    assert m.on_backend_exit(2, 5, True, 1, 1, False).action == "exit"        # flag off
    d = m.on_backend_exit(1, 0, False, 1, 1, True)                              # never ran
    assert (d.action, d.exit_code) == ("exit", 1)
    assert m.on_backend_exit(1, 0, True, 1, 1, True).action == "stay"          # refused after output
    assert m.on_backend_exit(1, 4, True, 1, 1, True).action == "retry"         # crash after output
    assert m.on_backend_exit(0, 0, False, 1, 1, True).action == "retry"


def test_flag_last_assignment_wins():
    assert m.pill_flag("A=1\nVOICEMODE_PILL=true\n")
    assert not m.pill_flag("VOICEMODE_PILL=true\nVOICEMODE_PILL=false\n")
    assert not m.pill_flag("")
    assert not m.pill_flag("#VOICEMODE_PILL=true")


def _listener():
    spec = importlib.util.spec_from_file_location("ptt_listener_macos", PATCHES / "ptt_listener_macos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_key_press_touches_the_pill_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.syspath_prepend(str(PATCHES))  # stands in for the installed voice_mode package
    mod = _listener()
    mod.touch_pill()
    assert (tmp_path / ".voicemode" / "pill" / "ptt").exists()
    monkeypatch.setenv("HOME", "/proc/nonexistent/x")  # unwritable: still silent
    mod.touch_pill()


def test_failed_spawns_back_off_and_do_not_recurse(tmp_path, monkeypatch):
    import inspect
    monkeypatch.setenv("HOME", str(tmp_path))
    app = m.App(True, ["/nonexistent"])
    app.started = 1.0  # a stale start time from an earlier run must not matter
    delays, depths = [], []

    def fake_sleep(s):
        delays.append(s)
        depths.append(len(inspect.stack()))
        if len(delays) == 8:
            app.closing = True

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(m.subprocess, "Popen", boom)
    monkeypatch.setattr(m.time, "sleep", fake_sleep)
    monkeypatch.setattr(m.time, "monotonic", lambda: 1000.0)  # old code: ran = 1000 s -> backoff reset
    app.start_backend()
    t = getattr(app, "thread", None)
    if t:
        t.join(5)
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    assert max(depths) - min(depths) <= 1


REPO = Path(__file__).resolve().parent.parent
WINDOW_CASES = [
    ("/Users/me/.voicemode/pill-venv/bin/python /r/patches/voice_pill_mac.py", True),
    ("/Users/me/.voicemode/pill-venv/bin/python /r/patches/voice_pill_mac.py --force", True),
    ("/L/Python.app/Contents/MacOS/Python /r/patches/voice_pill_mac.py", True),
    ("vim /r/patches/voice_pill_mac.py", False),
    ("/usr/bin/python3 -m pytest tests/test_voice_pill_mac.py", False),
    ("/usr/bin/python3 -m pytest /r/tests/test_voice_pill_mac.py -q", False),
    ("tail -f /h/.voicemode/logs/pill-window.log", False),
]
BACKEND_CASES = [
    ("/r/.venv/bin/python /r/patches/voice_pill.py --stdio", True),
    ("python3 -m voice_mode.voice_pill --stdio", True),
    ("vim /r/patches/voice_pill.py", False),
    ("/usr/bin/python3 -m pytest tests/test_voice_pill.py", False),
    ("/usr/bin/python3 -m pytest /r/tests/test_voice_pill_patch.py", False),
]


def _grep(regex, line):
    import subprocess
    return subprocess.run(["grep", "-E", regex], input=line + "\n", text=True,
                          capture_output=True).returncode == 0


def _script_regex(script, var):
    import re
    return re.search(rf"^{var}='(.*)'$", (REPO / script).read_text(), re.M).group(1)


def test_window_pattern_table():
    regex = _script_regex("voicemode-switch", "PILL_WINDOW_RE")
    for line, want in WINDOW_CASES:
        assert _grep(regex, line) == want, line


def test_launcher_uses_the_same_pattern_as_pill_off():
    regex = _script_regex("voicemode-switch", "PILL_WINDOW_RE")
    assert f"pgrep -f '{regex}'" in (REPO / "voicemode-mcp").read_text()


def test_backend_pattern_table():
    regex = _script_regex("voicemode-switch", "PILL_BACKEND_RE")
    for line, want in BACKEND_CASES:
        assert _grep(regex, line) == want, line


def test_page_is_loaded_from_a_file_url_and_no_http_server_is_asked_for():
    # pywebview starts its HTTP server for any plain path (is_local_url), even with
    # http_server=False. Only a file:// URL avoids the listener.
    ui = m.ui_file_url()
    assert ui.startswith("file:///") and ui.endswith("/ui/pill/index.html")
    assert m.START_OPTIONS == {"http_server": False}


def test_window_options_are_frameless_on_top_hidden_and_have_no_url_or_js_api():
    o = m.window_options()
    assert o["frameless"] and o["on_top"] and o["hidden"] and o["transparent"]
    assert "url" not in o and "js_api" not in o  # js_api needs eval; the page is loaded by the wrapper


def test_js_call_is_one_safe_expression():
    line = '{"label":"a\\u2028b</script>\'\\"x","n":1}\n'
    js = m.js_call("pillReceive", line)
    assert js.startswith("window.pillReceive&&window.pillReceive(") and js.endswith(")")
    assert "\n" not in js and "\u2028" not in js and "\u2029" not in js
    import json as _j
    arg = js[len("window.pillReceive&&window.pillReceive("):-1]
    assert _j.loads(arg) == line


def test_run_on_main_runs_inline_on_the_main_thread_and_is_scheduled_off_it():
    import threading
    ran_in = []

    def work():
        ran_in.append(threading.current_thread())
        return 7

    assert m.run_on_main(work, schedule=lambda f: (_ for _ in ()).throw(AssertionError("scheduled")),
                         is_main=lambda: True) == 7
    assert ran_in == [threading.current_thread()]

    main_stand_in = []

    def schedule(f):  # a stand-in "main thread": a second thread that runs it later
        t = threading.Thread(target=lambda: (main_stand_in.append(threading.current_thread()), f()))
        t.start()

    ran_in.clear()
    assert m.run_on_main(work, schedule=schedule, is_main=lambda: False) == 7
    assert ran_in == main_stand_in and ran_in[0] is not threading.current_thread()


def test_run_on_main_passes_exceptions_back():
    import pytest
    def bad():
        raise ValueError("x")
    with pytest.raises(ValueError):
        m.run_on_main(bad, schedule=lambda f: f(), is_main=lambda: False)


def test_docked_card_fills_the_window_height():
    css = (REPO / "ui" / "pill" / "pill.css").read_text()
    assert "[data-mode=docked] .card" in css and "100vh" in css
