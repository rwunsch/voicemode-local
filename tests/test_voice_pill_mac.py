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
    assert m.geometry(s, AREA) == (1440 - 350 - 16, 25 + 800 - 68 - 16, 350, 68)
    s.css_height = 200
    assert m.geometry(s, AREA, pos=(100, 100)) == (100, 100, 350, 200)
    s.mode, s.side = "docked", "right"
    assert m.geometry(s, AREA) == (1090, 325, 350, 200)  # centred on the edge, its own height
    s.side = "left"
    assert m.geometry(s, AREA) == (0, 325, 350, 200)


def test_saved_position_is_clamped_onto_the_screen():
    s = m.WindowState(visible=True, css_height=100)
    assert m.geometry(s, AREA, pos=(5000, -50)) == (1090, 25, 350, 100)
    assert m.geometry(s, AREA, pos=(-9, 5000)) == (0, 725, 350, 100)


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


def test_docked_card_keeps_its_own_height():
    css = (REPO / "ui" / "pill" / "pill.css").read_text()
    assert "[data-mode=docked] .card" not in css  # docked floats at its own height now


# Horizontal resize from the window edges
REPO = Path(__file__).resolve().parent.parent


def test_right_edge_changes_width_and_keeps_the_left_side_still():
    assert m.resized("right", 100, 320, 50, AREA) == (100, 370)
    assert m.resized("right", 100, 400, -20, AREA) == (100, 380)


def test_left_edge_keeps_the_right_side_still():
    assert m.resized("left", 100, 320, -50, AREA) == (50, 370)
    assert m.resized("left", 100, 400, 20, AREA) == (120, 380)


def test_resize_stops_at_the_width_limits():
    assert m.resized("right", 100, 320, -999, AREA) == (100, m.MIN_WIDTH)
    assert m.resized("right", 100, 320, 999, AREA) == (100, m.MAX_WIDTH)
    assert m.resized("left", 100, 320, 999, AREA) == (420 - m.MIN_WIDTH, m.MIN_WIDTH)


def test_resize_stops_at_the_screen_edge():
    assert m.resized("right", 1100, 280, 500, AREA) == (1100, 340)
    assert m.resized("left", 10, 320, -500, AREA) == (0, 330)


def test_geometry_uses_the_chosen_width_including_docked():
    s = m.WindowState(visible=True, css_width=500)
    assert m.geometry(s, AREA, pos=(100, 100))[2] == 500
    s.mode = "docked"
    assert m.geometry(s, AREA)[0::2] == (1440 - 500, 500)


def test_resize_message_is_recognised_and_changes_nothing_by_itself():
    s = m.WindowState(visible=True)
    assert s.on_window({"resize": "left"}) == "resize"
    assert s.css_width == m.WIDTH


def test_saved_width_is_read_back_and_limited():
    assert m.parse_saved_position("10 20 500 300") == ((10, 20), 500, 300)
    assert m.parse_saved_position("10 20 500") == ((10, 20), 500, None)
    assert m.parse_saved_position("10 20") == ((10, 20), None, None)
    assert m.parse_saved_position("10 20 9000") == ((10, 20), m.MAX_WIDTH, None)
    assert m.parse_saved_position("- - 400 300") == (None, 400, 300)
    assert m.parse_saved_position("junk") == (None, None, None)


def test_page_has_both_edges_and_windows_uses_the_same_limits():
    page = (REPO / "ui/pill/index.html").read_text()
    assert 'id="edgeL"' in page and 'id="edgeR"' in page
    js = (REPO / "ui/pill/app.js").read_text()
    assert 'resize: "left"' in js and 'resize: "right"' in js
    ps1 = (REPO / "pill-windows.ps1").read_text()
    assert f"MIN_WIDTH = {m.MIN_WIDTH}, MAX_WIDTH = {m.MAX_WIDTH}" in ps1


# Floating dock: on the screen edge, slides away to a small tab, moves only up and down
def test_tab_sits_on_the_edge_at_the_docked_height():
    s = m.WindowState(visible=True, css_height=200)
    s.mode = "docked"
    _, y, _, h = m.geometry(s, AREA)
    s.mode = "tab"
    x, ty, w, th = m.geometry(s, AREA)
    assert (x, w, th) == (1440 - m.TAB_W, m.TAB_W, m.TAB_H)
    assert ty + th // 2 == y + h // 2
    s.side = "left"
    assert m.geometry(s, AREA)[0] == 0


def test_dock_height_is_chosen_by_dragging_and_stays_on_screen():
    s = m.WindowState(visible=True, css_height=200, mode="docked", dock_y=300)
    assert m.geometry(s, AREA)[1] == 200
    s.dock_y = -500
    assert m.geometry(s, AREA)[1] == 25
    s.dock_y = 5000
    assert m.geometry(s, AREA)[1] == 625


def test_docked_ignores_the_saved_free_position():
    s = m.WindowState(visible=True, css_height=200, mode="docked")
    assert m.geometry(s, AREA, pos=(100, 100))[0] == 1090


def test_slide_message_is_recognised():
    assert m.WindowState().on_window({"slide": "out"}) == "slide"


def test_slide_steps_end_exactly_on_the_target():
    xs = m.slide_steps(1000, 1440)
    assert xs[-1] == 1440 and xs[0] > 1000 and xs == sorted(xs)


def test_page_has_the_tab_and_slide_and_windows_matches():
    page = (REPO / "ui/pill/index.html").read_text()
    assert 'id="tab"' in page and 'id="tuck"' in page
    assert 'slide: "out"' in (REPO / "ui/pill/app.js").read_text()
    ps1 = (REPO / "pill-windows.ps1").read_text()
    assert f"TAB_W = {m.TAB_W}, TAB_H = {m.TAB_H}" in ps1
    assert "SHAppBarMessage" not in ps1  # docking no longer takes screen space


# Minimized lives in the tray (Windows) or the menu bar (Mac), not on the desktop
def test_minimized_leaves_the_screen_for_the_tray():
    s = m.WindowState(visible=True, mode="dot")
    assert s.in_tray and not s.on_screen
    s.mode = "pill"
    assert s.on_screen and not s.in_tray


def test_no_tray_icon_when_the_pill_should_not_exist():
    assert not m.WindowState(visible=False, mode="dot").in_tray
    assert not m.WindowState(visible=True, mode="dot", dismissed=True).in_tray


def test_phase_message_is_kept_for_the_tray_colour():
    s = m.WindowState()
    assert s.on_window({"phase": "speaking"}) == "phase"
    assert s.phase == "speaking" and s.mode == "pill"


def test_tray_replaces_the_dot_on_both_platforms():
    assert 'id="dot"' not in (REPO / "ui/pill/index.html").read_text()
    js = (REPO / "ui/pill/app.js").read_text()
    assert '"restore"' in js and "phase: vm.phase" in js
    ps1 = (REPO / "pill-windows.ps1").read_text()
    assert "NotifyIcon" in ps1 and '{\\"cmd\\":\\"restore\\"}' in ps1


# Docked starts as a slim rail; the full panel opens from it
def test_rail_is_narrow_on_the_edge_at_its_own_height():
    s = m.WindowState(visible=True, css_height=210, css_width=500, mode="rail")
    x, y, w, h = m.geometry(s, AREA)
    assert (x, w, h) == (1440 - m.RAIL_W, m.RAIL_W, 210)
    assert y + h // 2 == 25 + 800 // 2
    s.side = "left"
    assert m.geometry(s, AREA)[0] == 0


def test_rail_keeps_the_dragged_height():
    s = m.WindowState(visible=True, css_height=200, mode="rail", dock_y=300)
    assert m.geometry(s, AREA)[1] == 200


def test_windows_uses_the_same_rail_width():
    assert f"RAIL_W = {m.RAIL_W}" in (REPO / "pill-windows.ps1").read_text()


# A slide still running when the tab is placed must not carry it off the monitor
def test_placing_the_window_stops_a_running_slide():
    ps1 = (REPO / "pill-windows.ps1").read_text()
    body = ps1[ps1.index("void Place() {"):]
    assert body.index("slideTimer.Stop()") < body.index("Move(new RECT")


# FELIX-MAC, e4729cd: back from the tab, height messages staled the slide-in after its jump
# off screen, so nothing brought the rail back. AppKit also refuses a fully off-screen move.
def _mac_method(name):
    mac = (REPO / "patches/voice_pill_mac.py").read_text()
    body = mac[mac.index(f"    def {name}(self"):]
    return body[:body.index("\n    def ", 10)]


def test_off_screen_target_keeps_one_point_on_screen():
    assert m.slide_off("right", AREA, 76) == 1439
    assert m.slide_off("left", AREA, 76) == -75


def test_only_a_mode_change_cancels_a_slide():
    body = _mac_method("from_page")
    assert '"mode" in r[1]' in body and "self.slide_gen += 1" in body
    assert "slide_gen" not in _mac_method("apply")


def test_slide_in_checks_for_a_newer_slide_before_its_jump_and_lands_exactly():
    body = _mac_method("slide")
    jump = body.index("self.place_off")
    assert "gen != self.slide_gen" in body[:jump]
    assert "self.apply()" in body[body.index("for x in"):]


def test_top_row_order_is_expand_dock_minimize():
    page = (REPO / "ui/pill/index.html").read_text()
    assert page.index('id="expand"') < page.index('id="dockTop"') < page.index('id="minimize"') < page.index('id="dismiss"') < page.index('id="more"')
    assert '$("dockTop").onclick' in (REPO / "ui/pill/app.js").read_text()
    css = (REPO / "ui/pill/pill.css").read_text()
    assert "[data-mode=docked] #dockTop" in css and "[data-mode=rail] #dockTop" in css


def test_narrowest_width_still_shows_the_status_text():
    assert m.MIN_WIDTH >= 320 and m.WIDTH >= 350


# From the first real two-session run, 2026-11-05
def test_stop_is_only_red_on_hover():
    css = (REPO / "ui/pill/pill.css").read_text()
    assert ".stop{" not in css and ".stop:hover" in css


def test_pickers_let_go_after_a_choice_and_when_the_floor_moves():
    # A focused select is not refreshed, so it kept showing the last session's voice.
    js = (REPO / "ui/pill/app.js").read_text()
    assert "this.blur()" in js
    assert "floorPid" in js and "document.activeElement.blur()" in js


def test_session_name_has_its_own_line():
    page = (REPO / "ui/pill/index.html").read_text()
    assert page.index('id="sub"') < page.index('id="label"') < page.index('id="interrupt"')


def test_default_height_matches_the_two_line_pill_on_both_platforms():
    assert m.DEFAULT_H == 68
    ps1 = (REPO / "pill-windows.ps1").read_text()
    assert f"DEFAULT_H = {m.DEFAULT_H}" in ps1 and "Scale(46)" not in ps1


def test_waiting_count_sits_on_the_name_line():
    page = (REPO / "ui/pill/index.html").read_text()
    assert page.index('id="sub"') < page.index('id="waiting"') < page.index('id="label"')


# No focus, no Dock icon, a menu bar icon that can be seen (Robert's real-mouse run, 69303d6)
def _main_src():
    src = (REPO / "patches/voice_pill_mac.py").read_text()
    return src[src.index("def main("):]


def test_app_is_made_quiet_before_pywebview_can_set_regular():
    src = _main_src()
    assert src.index("quiet_app()") < src.index("import webview")


def test_quiet_app_forces_accessory_and_refuses_activation():
    src = (REPO / "patches/voice_pill_mac.py").read_text()
    body = src[src.index("def quiet_app("):src.index("def use_panel(")]
    assert 'info["LSUIElement"] = "1"' in body
    assert body.index("LSUIElement") < body.index("from AppKit")
    assert "def setActivationPolicy_" in body and "def activateIgnoringOtherApps_" in body
    assert body.index("LSUIElement") < body.index(".sharedApplication()")


def test_panel_is_set_before_the_window_is_created():
    src = _main_src()
    assert src.index("import webview") < src.index("use_panel()") < src.index("create_window")


def test_panel_is_nonactivating_from_birth_and_never_key():
    src = (REPO / "patches/voice_pill_mac.py").read_text()
    body = src[src.index("def use_panel("):src.index("class App:")]
    assert m.NONACTIVATING_PANEL == 1 << 7
    assert "mask | NONACTIVATING_PANEL" in body
    assert "def canBecomeKeyWindow" in body and "return False" in body
    assert "def acceptsFirstMouse_" in body and "return True" in body
    # PyObjC refuses a Category whose name differs from its class, and the app then exits at launch
    assert "class WebKitHost(objc.Category(BrowserView.WebKitHost)):" in body
    assert "BrowserView.WindowHost = PillPanel" in body


def test_nothing_hands_focus_back_any_more():
    assert "activateWithOptions_" not in (REPO / "patches/voice_pill_mac.py").read_text()


def test_window_joins_all_spaces_but_not_full_screen():
    assert m.WINDOW_BEHAVIOUR == 1 << 0


def test_menu_bar_ball_uses_the_windows_tray_colours():
    assert m.tray_rgb("speaking") == (139, 92, 246)
    assert m.tray_rgb("listening") == (13, 148, 136)
    assert m.tray_rgb("working") == m.tray_rgb(None) == (148, 163, 184)
    ps1 = (REPO / "pill-windows.ps1").read_text()
    assert "FromArgb(139, 92, 246)" in ps1 and "FromArgb(13, 148, 136)" in ps1 and "FromArgb(148, 163, 184)" in ps1


def test_menu_bar_ball_is_outlined_and_not_a_template():
    src = (REPO / "patches/voice_pill_mac.py").read_text()
    assert "setTemplate_" not in src and "imageWithSystemSymbolName" not in src
    assert "path.fill()" in src and "path.stroke()" in src


# A panel that is never key gets no hover from WebKit: the wrapper reports the pointer itself
def test_hover_message_is_one_safe_expression():
    assert m.hover_js(12.5, 30) == "window.pillHover&&window.pillHover(12.5,30)"
    assert m.hover_js(None, None) == "window.pillHover&&window.pillHover(-1,-1)"


def test_resize_cursor_only_on_the_edges_of_a_resizable_window():
    assert m.on_resize_edge(9, 350, "pill") and m.on_resize_edge(341, 350, "expanded")
    assert not m.on_resize_edge(11, 350, "pill") and not m.on_resize_edge(339, 350, "pill")
    for mode in ("docked", "rail", "tab", "dot"):
        assert not m.on_resize_edge(2, 350, mode)


def test_edge_grab_area_is_the_same_width_in_page_and_wrapper():
    css = (REPO / "ui/pill/pill.css").read_text()
    assert f".edge{{position:fixed;top:0;bottom:0;width:{m.EDGE_W}px" in css
    assert m.EDGE_W == 10


def test_tracking_area_works_while_the_app_is_inactive():
    src = (REPO / "patches/voice_pill_mac.py").read_text()
    body = src[src.index("def install_bridge_and_load("):src.index("def drag(self")]
    assert "NSTrackingActiveAlways" in body and "NSTrackingMouseMoved" in body and "addTrackingArea_" in body


def test_page_has_a_hover_class_for_every_hover_rule():
    css = (REPO / "ui/pill/pill.css").read_text()
    assert ".btn.hover:not(:disabled)" in css and ".stop.hover:not(:disabled)" in css and ".tab.hover" in css
    assert ".edge.hover::after" in css and ".edge:hover::after" in css
    js = (REPO / "ui/pill/app.js").read_text()
    assert "window.pillHover" in js and 'closest(".btn,.tab,.edge")' in js


def test_resize_logs_what_it_did():
    src = (REPO / "patches/voice_pill_mac.py").read_text()
    body = src[src.index("def resize_loop("):src.index("def shutdown(self")]
    assert "self.log(" in body


# Robert, 69612df: a flash of the full window before it slides in; FELIX-MAC measured about 30 ms
def test_return_from_the_tab_never_places_the_window_on_screen_first():
    body = _mac_method("from_page")
    branch = body[body.index('prev == "tab"'):body.index("self.apply()")]
    assert "self.sliding = True" in branch and "args=(False,)" in branch
    assert "else:" in branch  # apply() is only the other branch
    place = _mac_method("place_off")
    assert place.index("setFrame_display_") < place.index("orderFrontRegardless")
    assert "NSMakeRect(off, y, w, h)" in place
    assert "time.sleep(0.03)" not in _mac_method("slide")


# FELIX-MAC, 69612df: AppKit's own edge resize went past the limit, frame 885x100
def test_appkit_cannot_resize_the_window_itself():
    assert m.window_options()["resizable"] is False


def test_resize_log_shows_whole_pixels():
    assert "int(last)" in _mac_method("resize_loop")


def test_a_mode_change_clears_a_stale_hover():
    js = (REPO / "ui/pill/app.js").read_text()
    body = js[js.index("if (vm.mode !== lastMode)"):]
    assert "window.pillHover(-1, -1)" in body[:body.index("\n")]
