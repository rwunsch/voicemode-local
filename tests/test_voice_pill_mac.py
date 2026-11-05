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
