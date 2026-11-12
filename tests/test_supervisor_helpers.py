"""vml_supervisor.helpers: the barge trigger and the push-to-talk listener.

Ported from FELIX 454f20d0e tests/repo/test_voicemode_setup.py: the `_helper_world` fakes and the
helper tests, under the same names. FELIX's ensure_barge / ensure_ptt / stop_barge become
start_helper / stop_helper / stop_unrecorded driven by the profile's HelperSpec, and FELIX's
"None or a problem string" return becomes a Result.

Mapping (FELIX test -> what it checks here):
  test_push_to_talk_starts_on_windows_beside_the_services: a helper whose platforms exclude this
      one is not started (helper_not_for_platform); on its platform it starts from HelperSpec.argv.
  test_a_changed_barge_mode_restarts_the_trigger: same mode is a no-op, a new mode restarts.
  test_an_unrecorded_helper_of_this_install_is_stopped_before_a_new_one: root + os.sep boundary.
  test_a_helper_that_stops_at_once_is_reported_not_recorded: helper_exited, not recorded.
  test_a_helper_that_cannot_be_started_never_stops_voice: OSError gives helper_failed.
  test_a_mac_helper_does_not_inherit_held_stop_signals: preexec_fn unblocks the stop signals.
  test_switching_off_stops_the_trigger_even_if_the_setting_cannot_be_written: FELIX's settings
      write stays FELIX's; here, stop_helper stops the recorded trigger and stop_unrecorded this
      install's unrecorded ones, independently of anything the profile owner writes after.
"""
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vml_supervisor import helpers
from vml_supervisor.hooks import Hooks
from vml_supervisor.profile import HelperSpec, HostProfile, PortOverride, ServiceSpec

BARGE_MARKER = "voice_mode.voice_barge"
PTT_MARKER = "ptt-listener-windows.ps1"


def make_profile(root: Path, **helper_over) -> HostProfile:
    barge = HelperSpec(["python", "-m", BARGE_MARKER, str(root / "runtimes/r/models/silero/silero_vad.onnx")],
                       BARGE_MARKER, "barge.log", (), True)
    ptt = HelperSpec(["powershell.exe", "-NoProfile", "-File", str(root / "vendor" / PTT_MARKER),
                      "-FocusPattern", "^github$"], PTT_MARKER, "ptt-listener.log", ("win32",), False)
    specs = {"barge": barge, "ptt": ptt}
    specs.update(helper_over)
    return HostProfile(
        name="test", root=root, run_dir=root / "run", runtime_dir=root / "runtimes/r",
        python=Path(sys.executable), working_dir=root / "work", env={"A": "1"},
        services={"tts": ServiceSpec(["tts", "--port", "{port}"], "tts", (28880, 28899), "/v1/models", "kokoro")},
        helpers=specs, reserved_ports=(), port_override=PortOverride("none"),
        start_timeout=5.0, stop_timeout=2.0, watch_interval=5.0, restart_limit=(3, 60.0),
        lease_markers=("lease",), voice_server_marker="voice-server", host_command=[], host_marker="",
        watch_command=[], watch_marker="", job_name="vml-test-job")


def _helper_world(tmp_path, monkeypatch, *, exits=None, running=(), **helper_over):
    """A voice folder, a fake process table and a Popen that starts nothing."""
    root = tmp_path / "voice"
    (root / "run").mkdir(parents=True)
    stopped, started = [], []
    monkeypatch.setattr(helpers, "background_options", lambda: {})
    monkeypatch.setattr(helpers, "HELPER_START_CHECK", 0.05)
    monkeypatch.setattr(helpers, "running_commands", lambda: list(running))
    monkeypatch.setattr(helpers, "stop_entry", lambda profile, entry: stopped.append(entry["pid"]))
    monkeypatch.setattr(helpers, "owned", lambda entry: entry is not None and entry.get("pid") not in stopped)
    options = []

    def popen(args, **kw):
        if exits == "raise":
            raise FileNotFoundError(2, "No such file", args[0])
        started.append(args)
        options.append(kw)
        return SimpleNamespace(pid=900 + len(started), poll=lambda: exits, returncode=exits)

    monkeypatch.setattr(helpers.subprocess, "Popen", popen)
    world = SimpleNamespace(profile=make_profile(root, **helper_over), root=root, started=started,
                            stopped=stopped, options=options)
    return world


def state_of(world):
    return json.loads((world.root / "run/state.json").read_text())


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_push_to_talk_starts_on_windows_beside_the_services(tmp_path, monkeypatch, platform):
    """Robert, 2026-11-11: connect push-to-talk. The listener starts only where its HelperSpec says
    (Windows); elsewhere nothing is started and the Result says why."""
    world = _helper_world(tmp_path, monkeypatch)
    monkeypatch.setattr(helpers, "PLATFORM", platform)
    monkeypatch.setattr(helpers, "WINDOWS", False)   # no real Job Object off Windows
    result = helpers.start_helper(world.profile, "ptt", None)
    if platform != "win32":
        assert result.code == "helper_not_for_platform" and world.started == []
        return
    assert result.code == "helper_started"
    (command,) = world.started
    assert command == world.profile.helpers["ptt"].argv
    assert command[command.index("-FocusPattern") + 1] == "^github$"
    assert world.options[0]["cwd"] == str(world.profile.working_dir)
    entry = state_of(world)["ptt"]
    assert entry["pid"] == 901 and entry["marker"] == PTT_MARKER and entry["port"] is None
    assert "ptt-listener.log" in result.detail and "started" in result.detail


def test_a_changed_barge_mode_restarts_the_trigger(tmp_path, monkeypatch):
    """Review, 2026-11-11: the trigger reads auto or on only when it starts, so a switch must restart it."""
    world = _helper_world(tmp_path, monkeypatch)
    assert helpers.start_helper(world.profile, "barge", "auto").code == "helper_started"
    assert helpers.start_helper(world.profile, "barge", "auto").code == "helper_running"
    assert len(world.started) == 1 and world.stopped == []           # same mode: left running
    assert helpers.start_helper(world.profile, "barge", "on").code == "helper_restarted"
    assert len(world.started) == 2 and world.stopped == [901]        # new mode: restarted
    assert state_of(world)["barge"]["mode"] == "on"


def test_an_unrecorded_helper_of_this_install_is_stopped_before_a_new_one(tmp_path, monkeypatch):
    """Review, 2026-11-11: a watchdog from before an update cleared state.json without stopping the helpers, and
    the next start added a second key listener. Only this install's helpers count."""
    root = tmp_path / "voice"
    running = [(51, f"python -m voice_mode.voice_barge {root}/runtimes/r/models/silero/silero_vad.onnx"),
               (52, "python -m voice_mode.voice_barge /home/someone/.voicemode/silero.onnx"),
               (53, f"python -m voice_mode.voice_barge {root}-old/runtimes/r/models/silero/silero_vad.onnx")]
    world = _helper_world(tmp_path, monkeypatch, running=running)
    result = helpers.start_helper(world.profile, "barge", "auto")
    assert world.stopped == [51] and len(world.started) == 1
    assert [r.code for r in result.data["results"]] == ["helper_unrecorded_stopped"]


def test_a_helper_that_stops_at_once_is_reported_not_recorded(tmp_path, monkeypatch):
    """Review, 2026-11-11: a trigger or listener that exited at once went unnoticed, and voice said it was on."""
    world = _helper_world(tmp_path, monkeypatch, exits=1)
    result = helpers.start_helper(world.profile, "barge", "auto")
    assert result.code == "helper_exited"
    assert "exit code 1" in result.detail and "barge.log" in result.detail and result.data["exit_code"] == 1
    assert "barge" not in state_of(world)


def test_a_helper_that_cannot_be_started_never_stops_voice(tmp_path, monkeypatch):
    """Second review, 2026-11-11: an OSError from Popen (powershell.exe missing or blocked) ended the connector."""
    world = _helper_world(tmp_path, monkeypatch, exits="raise")
    result = helpers.start_helper(world.profile, "barge", "auto")
    assert result.code == "helper_failed" and "No such file" in result.detail
    assert "barge" not in state_of(world)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal mask")
def test_a_mac_helper_does_not_inherit_held_stop_signals(tmp_path, monkeypatch):
    """Second review, 2026-11-11: started while stop signals were held, the trigger could only be killed."""
    world = _helper_world(tmp_path, monkeypatch)
    helpers.start_helper(world.profile, "barge", "auto")
    assert world.options[0].get("preexec_fn") is helpers.unblock_stop_signals


def test_switching_off_stops_the_trigger_even_if_the_setting_cannot_be_written(tmp_path, monkeypatch):
    """Talos, 2026-11-12: the off choice was saved, the settings write failed, and the trigger kept listening.
    The stop is the package's and comes first; the settings write that may fail after it is the owner's."""
    root = tmp_path / "voice"
    running = [(61, f"python -m voice_mode.voice_barge {root}/runtimes/old/models/silero/silero_vad.onnx")]
    world = _helper_world(tmp_path, monkeypatch, running=running)
    helpers.start_helper(world.profile, "barge", "auto")
    world.stopped.clear()
    stopped = helpers.stop_helper(world.profile, "barge")
    assert stopped.code == "helper_stopped" and stopped.detail.startswith("stopped")
    assert "barge" not in state_of(world) and world.stopped == [901]
    unrecorded = helpers.stop_unrecorded(world.profile, "barge")
    assert [r.code for r in unrecorded] == ["helper_unrecorded_stopped"] and world.stopped == [901, 61]
    assert helpers.stop_helper(world.profile, "barge").code == "helper_not_running"


def test_read_mode_comes_from_the_hook_and_defaults_without_one(tmp_path, monkeypatch):
    """The mode is the profile owner's: its read_mode hook says it; no hook means None (the helper's default).
    A failing hook gives the default, and start_helper passes the failure on in its results."""
    world = _helper_world(tmp_path, monkeypatch)
    profile = world.profile
    assert helpers.read_mode(profile, "barge", hooks=Hooks()) is None
    seen = []
    hooks = Hooks({"read_mode": lambda profile_dict, kind: seen.append((profile_dict["name"], kind)) or "on"})
    assert helpers.read_mode(profile, "barge", hooks=hooks) == "on" and seen == [("test", "barge")]
    assert helpers.start_helper(profile, "barge", hooks=hooks).code == "helper_started"
    assert state_of(world)["barge"]["mode"] == "on"

    def broken(profile_dict, kind):
        raise RuntimeError("preferences unreadable")
    problems = []
    assert helpers.read_mode(profile, "barge", hooks=Hooks({"read_mode": broken}), results=problems) is None
    assert [r.code for r in problems] == ["hook_failed"]
    result = helpers.start_helper(profile, "barge", hooks=Hooks({"read_mode": broken}))
    assert result.code == "helper_restarted"                      # "on" -> the default (None)
    assert "hook_failed" in [r.code for r in result.data["results"]]


# ----- beyond FELIX ---------------------------------------------------------------------------


def test_a_helper_with_an_empty_marker_is_not_started_and_never_sweeps(tmp_path, monkeypatch):
    """An empty marker is in every command line: it could never be owned, and stop_unrecorded would stop
    every process in the install folder."""
    world = _helper_world(tmp_path, monkeypatch, running=[(71, "anything at all")],
                          barge=HelperSpec(["python", "barge.py"], "", "barge.log", (), True))
    assert helpers.start_helper(world.profile, "barge", "auto").code == "helper_command_bad"
    assert [r.code for r in helpers.stop_unrecorded(world.profile, "barge")] == ["helper_command_bad"]
    assert world.started == [] and world.stopped == []


def test_a_helper_whose_marker_is_not_in_its_command_is_not_started(tmp_path, monkeypatch):
    world = _helper_world(tmp_path, monkeypatch,
                          barge=HelperSpec(["python", "barge.py"], "elsewhere.py", "barge.log", (), True))
    assert helpers.start_helper(world.profile, "barge", "auto").code == "helper_command_unowned"
    assert world.started == []


def test_the_mode_fills_its_placeholder_and_a_helper_without_a_mode_ignores_it(tmp_path, monkeypatch):
    world = _helper_world(tmp_path, monkeypatch,
                          barge=HelperSpec(["python", "barge.py", "--mode={mode}"], "barge.py", "b.log", (), True),
                          ptt=HelperSpec(["python", "ptt.py", "{mode}"], "ptt.py", "p.log", (), False))
    helpers.start_helper(world.profile, "barge", "on")
    assert world.started[-1] == ["python", "barge.py", "--mode=on"]
    helpers.start_helper(world.profile, "barge", None)               # the default: placeholder dropped
    assert world.started[-1] == ["python", "barge.py"]
    helpers.start_helper(world.profile, "ptt", "on")                 # needs_mode False: mode ignored
    assert world.started[-1] == ["python", "ptt.py"] and state_of(world)["ptt"]["mode"] is None
    assert helpers.start_helper(world.profile, "ptt", "off").code == "helper_running"


def test_an_unknown_helper_is_a_result(tmp_path, monkeypatch):
    world = _helper_world(tmp_path, monkeypatch)
    assert helpers.start_helper(world.profile, "nope", None).code == "unknown_helper"
    assert helpers.stop_helper(world.profile, "nope").code == "unknown_helper"
    assert [r.code for r in helpers.stop_unrecorded(world.profile, "nope")] == ["unknown_helper"]


@pytest.mark.skipif(sys.platform == "win32", reason="acts out POSIX with a real child")
def test_a_real_helper_starts_in_the_working_dir_and_stops(tmp_path, monkeypatch):
    """No fakes: a real child records its pid, runs in profile.working_dir, logs to run/<log>, and stops."""
    monkeypatch.setattr(helpers, "HELPER_START_CHECK", 0.3)
    root = tmp_path / "voice"
    script = "import os, time; print(os.getcwd(), flush=True); time.sleep(60)"
    marker = "vml-helper-test-" + str(os.getpid())
    profile = make_profile(root, barge=HelperSpec([sys.executable, "-c", script, marker, str(root)], marker,
                                                  "barge.log", (), True))
    result = helpers.start_helper(profile, "barge", "auto")
    assert result.code == "helper_started", result
    pid = result.data["pid"]
    try:
        deadline = time.monotonic() + 5
        log = root / "run/barge.log"
        while time.monotonic() < deadline and not (log.is_file() and log.read_text().strip()):
            time.sleep(0.05)
        assert log.read_text().strip() == str(profile.working_dir)
        assert helpers.start_helper(profile, "barge", "auto").code == "helper_running"
    finally:
        stopped = helpers.stop_helper(profile, "barge")
    assert stopped.code == "helper_stopped"
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
