"""Tests for voicemode_sysaudio — cross-OS 'fold system output into voice input'.

Pure-logic tests: platform detection, config loading, and per-backend command
construction with an injected fake command runner (no real audio devices touched).
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

import voicemode_sysaudio as sa  # noqa: E402
from voicemode_sysaudio import detect, config  # noqa: E402
from voicemode_sysaudio.backends import linux as linux_be  # noqa: E402
from voicemode_sysaudio.backends import wsl as wsl_be  # noqa: E402
from voicemode_sysaudio.backends import windows as win_be  # noqa: E402


# ---------- platform detection ----------

def test_detect_windows():
    assert detect.platform_kind(sys_platform="win32") == "windows"


def test_detect_macos():
    assert detect.platform_kind(sys_platform="darwin") == "darwin"


def test_detect_native_linux():
    assert detect.platform_kind(
        sys_platform="linux", uname_release="6.6.0-generic", environ={}) == "linux"


def test_detect_wsl_via_uname():
    assert detect.platform_kind(
        sys_platform="linux",
        uname_release="6.6.87.2-microsoft-standard-WSL2", environ={}) == "wsl"


def test_detect_wsl_via_env():
    assert detect.platform_kind(
        sys_platform="linux", uname_release="6.6.0",
        environ={"WSL_DISTRO_NAME": "Ubuntu"}) == "wsl"


# ---------- config ----------

def test_config_defaults():
    c = config.Config.load(environ={}, path=Path("/nonexistent.json"))
    assert c.mix_sink == "voicemode_mix"
    assert c.vm_bus == "B1"
    assert c.vm_strip is None


def test_config_env_override():
    c = config.Config.load(environ={
        "VOICEMODE_SYSAUDIO_MIX_SINK": "mymix",
        "VOICEMODE_SYSAUDIO_VM_STRIP": "3",
        "VOICEMODE_SYSAUDIO_VM_BUS": "B2",
    }, path=Path("/nonexistent.json"))
    assert c.mix_sink == "mymix"
    assert c.vm_strip == 3
    assert c.vm_bus == "B2"


def test_config_json_file(tmp_path):
    p = tmp_path / "sysaudio.json"
    p.write_text(json.dumps({"win_python": "/mnt/c/py.exe",
                             "helper_path": r"C:\vm.py", "vm_strip": 4}))
    c = config.Config.load(environ={}, path=p)
    assert c.win_python == "/mnt/c/py.exe"
    assert c.helper_path == r"C:\vm.py"
    assert c.vm_strip == 4


def test_config_env_beats_json(tmp_path):
    p = tmp_path / "sysaudio.json"
    p.write_text(json.dumps({"vm_strip": 4}))
    c = config.Config.load(environ={"VOICEMODE_SYSAUDIO_VM_STRIP": "7"}, path=p)
    assert c.vm_strip == 7


# ---------- linux backend (fake pactl runner) ----------

class FakeRun:
    """Records commands, returns canned pactl output."""
    def __init__(self, modules_short=""):
        self.calls = []
        self.modules_short = modules_short
        self.default_sink = "RDPSink"
        self.default_source = "RDPSource"
        self.next_module_id = 42

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:4] == ["pactl", "list", "short", "modules"]:
            return (0, self.modules_short, "")
        if cmd[:2] == ["pactl", "get-default-sink"]:
            return (0, self.default_sink, "")
        if cmd[:2] == ["pactl", "get-default-source"]:
            return (0, self.default_source, "")
        if len(cmd) > 1 and cmd[1] == "load-module":
            mid = str(self.next_module_id)
            self.next_module_id += 1
            return (0, mid, "")
        return (0, "", "")

    def loaded(self, *needles):
        return [c for c in self.calls
                if len(c) > 1 and c[1] == "load-module"
                and all(any(n in a for a in c) for n in needles)]

    def unloaded(self):
        return [c for c in self.calls if len(c) > 1 and c[1] == "unload-module"]


def _cfg():
    return config.Config.load(environ={}, path=Path("/nonexistent.json"))


def test_find_module_matches_by_name_and_args():
    text = ("10\tmodule-null-sink\tsink_name=voicemode_mix\n"
            "11\tmodule-loopback\tsource=RDPSink.monitor sink=voicemode_mix\n")
    assert linux_be._find_module(text, "module-loopback",
                                 "source=RDPSink.monitor", "sink=voicemode_mix") == "11"
    assert linux_be._find_module(text, "module-loopback",
                                 "source=other") is None


def test_linux_on_creates_mix_and_taps_monitor():
    fake = FakeRun(modules_short="")          # nothing loaded yet
    r = linux_be.apply("on", _cfg(), run=fake)
    assert r.state == "on" and r.ok
    # null sink created, mic loopback created, system monitor tap created
    assert fake.loaded("module-null-sink", "sink_name=voicemode_mix")
    assert fake.loaded("module-loopback", "source=RDPSource", "sink=voicemode_mix")
    assert fake.loaded("module-loopback", "source=RDPSink.monitor", "sink=voicemode_mix")


def test_linux_on_is_idempotent_when_tap_present():
    pre = ("10\tmodule-null-sink\tsink_name=voicemode_mix\n"
           "11\tmodule-loopback\tsource=RDPSource sink=voicemode_mix\n"
           "12\tmodule-loopback\tsource=RDPSink.monitor sink=voicemode_mix\n")
    fake = FakeRun(modules_short=pre)
    r = linux_be.apply("on", _cfg(), run=fake)
    assert r.state == "on" and r.ok
    assert fake.loaded() == []                 # nothing re-created


def test_linux_off_unloads_only_the_monitor_tap():
    pre = ("10\tmodule-null-sink\tsink_name=voicemode_mix\n"
           "11\tmodule-loopback\tsource=RDPSource sink=voicemode_mix\n"
           "12\tmodule-loopback\tsource=RDPSink.monitor sink=voicemode_mix\n")
    fake = FakeRun(modules_short=pre)
    r = linux_be.apply("off", _cfg(), run=fake)
    assert r.state == "off" and r.ok
    assert fake.unloaded() == [["pactl", "unload-module", "12"]]


def test_linux_status_reports_on_off():
    on = ("12\tmodule-loopback\tsource=RDPSink.monitor sink=voicemode_mix\n")
    assert linux_be.apply("status", _cfg(), run=FakeRun(modules_short=on)).state == "on"
    assert linux_be.apply("status", _cfg(), run=FakeRun(modules_short="")).state == "off"


# ---------- wsl backend (shells to Windows helper) ----------

def test_wsl_unconfigured_reports_setup_needed():
    r = wsl_be.apply("on", _cfg(), run=lambda cmd: (0, "", ""))
    assert not r.ok
    assert "config" in r.detail.lower() or "setup" in r.detail.lower()


def test_wsl_builds_windows_command():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return (0, "on", "")

    c = config.Config.load(environ={
        "VOICEMODE_SYSAUDIO_WIN_PYTHON": "/mnt/c/py.exe",
        "VOICEMODE_SYSAUDIO_HELPER_PATH": r"C:\vm_sysaudio.py",
        "VOICEMODE_SYSAUDIO_VM_STRIP": "3",
    }, path=Path("/nonexistent.json"))
    r = wsl_be.apply("on", c, run=run)
    assert r.ok and r.state == "on"
    assert calls == [["/mnt/c/py.exe", r"C:\vm_sysaudio.py", "on", "3"]]


# ---------- windows backend (pure param construction) ----------

def test_windows_param_name():
    c = config.Config.load(environ={
        "VOICEMODE_SYSAUDIO_VM_STRIP": "4",
        "VOICEMODE_SYSAUDIO_VM_BUS": "B1",
    }, path=Path("/nonexistent.json"))
    assert win_be.param_name(c) == "Strip[4].B1"


# ---------- dispatch ----------

def test_get_backend_per_platform():
    for kind in ("linux", "wsl", "windows", "darwin"):
        be = sa.get_backend(kind)
        assert hasattr(be, "apply"), f"{kind} backend missing apply()"


def test_set_system_audio_routes_to_kind(monkeypatch):
    seen = {}

    class FakeBackend:
        @staticmethod
        def apply(state, cfg, run=None):
            seen["state"] = state
            return sa.Result(ok=True, state=state, detail="", platform="linux")

    monkeypatch.setattr(sa, "get_backend", lambda kind: FakeBackend)
    r = sa.set_system_audio("on", cfg=_cfg(), kind="linux")
    assert r.ok and seen["state"] == "on"
