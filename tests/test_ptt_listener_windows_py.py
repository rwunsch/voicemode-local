"""patches/ptt_listener_windows.py, the legacy Python Windows companion, checked on Linux.

Talos re-review 2026-11-13 (M1). The module imports pywin32 and pynput at the top, so
those are stubbed here; the key handling runs through a fake keyboard.Listener and the
wire through a real patches/ptt_relay.py server on loopback.
"""
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
TOKEN = "ab" * 32


class _Key:
    ctrl = "ctrl"
    ctrl_l = "ctrl_l"
    ctrl_r = "ctrl_r"
    space = "space"


class _Listener:
    script = []   # (press|release, key) pairs, played during join()

    def __init__(self, on_press, on_release):
        self.on_press, self.on_release = on_press, on_release

    def start(self):
        pass

    def join(self):
        for kind, key in self.script:
            if kind == "interrupt":
                raise KeyboardInterrupt
            (self.on_press if kind == "press" else self.on_release)(key)

    def stop(self):
        pass


def _load(monkeypatch):
    keyboard = types.SimpleNamespace(Key=_Key, Listener=_Listener)
    pynput = types.ModuleType("pynput")
    pynput.keyboard = keyboard
    monkeypatch.setitem(sys.modules, "pynput", pynput)
    monkeypatch.setitem(sys.modules, "win32gui", types.SimpleNamespace(GetForegroundWindow=lambda: 1))
    monkeypatch.setitem(sys.modules, "win32process",
                        types.SimpleNamespace(GetWindowThreadProcessId=lambda h: (0, 0)))
    spec = importlib.util.spec_from_file_location("vml_ptt_listener_windows_py", PATCHES / "ptt_listener_windows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "focused_window_is_terminal", lambda: True)
    return mod


def _run(monkeypatch, script):
    mod = _load(monkeypatch)
    sent = []
    monkeypatch.setattr(mod, "send_event", lambda action: sent.append(action.value))
    monkeypatch.setattr(_Listener, "script", script)
    mod.main()
    return sent


def test_psutil_is_in_the_documented_install():
    # focused_window_is_terminal imports psutil; without it every press is dropped silently.
    text = (PATCHES / "ptt_listener_windows.py").read_text(encoding="utf-8")
    assert "import psutil" in text
    assert "pip install pynput pywin32 psutil" in text


def test_releasing_one_ctrl_keeps_the_other_held(monkeypatch):
    # Both Ctrl keys down, the left one let go: the right one still holds Ctrl.
    sent = _run(monkeypatch, [("press", "ctrl_l"), ("press", "ctrl_r"), ("release", "ctrl_l"),
                              ("press", "space"), ("release", "space")])
    assert sent and sent[0] == "hold_start", sent


def test_releasing_the_only_ctrl_ends_the_chord(monkeypatch):
    sent = _run(monkeypatch, [("press", "ctrl_l"), ("release", "ctrl_l"), ("press", "space")])
    assert sent == []


def test_stopping_mid_hold_sends_hold_release(monkeypatch):
    # The server's hold stays latched with the mic open unless the stop ends it.
    sent = _run(monkeypatch, [("press", "ctrl_l"), ("press", "space"), ("interrupt", None)])
    assert sent == ["hold_start", "hold_release"]


def test_stopping_with_no_hold_sends_nothing(monkeypatch):
    sent = _run(monkeypatch, [("press", "ctrl_l"), ("interrupt", None)])
    assert sent == []


@pytest.fixture
def relay():
    relay_spec = importlib.util.spec_from_file_location("vml_ptt_relay_for_py_listener", PATCHES / "ptt_relay.py")
    ptt_relay = importlib.util.module_from_spec(relay_spec)
    relay_spec.loader.exec_module(ptt_relay)
    got = []
    done = threading.Event()

    class Client:
        def action_outcome(self, action):
            got.append(action)
            done.set()
            return "ok"

    srv = ptt_relay.make_server("127.0.0.1", 0, TOKEN, client=Client())
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1], got, done
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_the_relay_accepts_what_the_listener_sends(monkeypatch, tmp_path, relay):
    # The relay closes a connection whose first line is not "token <hex>".
    port, got, done = relay
    token_file = tmp_path / "ptt-relay.token"
    token_file.write_text(TOKEN + "\n", encoding="ascii")
    mod = _load(monkeypatch)
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setenv("VOICEMODE_PTT_TOKEN_PATH", str(token_file))
    mod.send_event(mod.PTTAction.HOLD_START)
    assert done.wait(5), "the relay forwarded nothing"
    assert got == ["hold_start"]


def test_without_a_token_nothing_is_sent(monkeypatch, tmp_path, relay):
    port, got, done = relay
    mod = _load(monkeypatch)
    monkeypatch.setattr(mod, "PORT", port)
    monkeypatch.setenv("VOICEMODE_PTT_TOKEN_PATH", str(tmp_path / "missing.token"))
    connects = []
    real = mod.socket.create_connection
    monkeypatch.setattr(mod.socket, "create_connection", lambda *a, **k: connects.append(a) or real(*a, **k))
    mod.send_event(mod.PTTAction.HOLD_START)
    assert connects == [] and got == []
