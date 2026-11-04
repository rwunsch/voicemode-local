"""Behaviour of patch_voice_barge on pristine 8.12.0 sources.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_voice_barge_patch.py
"""
import ast
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
FILES = ("control_channel.py", "control_socket.py", "core.py", "converse.py")


def _patch(script, target):
    r = subprocess.run([sys.executable, str(PATCHES / script), str(target)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"{script} on {target.name}: {r.stderr}"
    return r.stdout


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    d = tmp_path_factory.mktemp("barge")
    for f in FILES:
        shutil.copy(Path(raw) / f, d / f)
    # Same order as apply.sh: this patch sits on top of the hold patches.
    _patch("patch_control_hold.py", d / "control_channel.py")
    _patch("patch_converse_hold.py", d / "converse.py")
    _patch("patch_hold_barges_in.py", d / "core.py")
    _patch("patch_hold_barges_in.py", d / "converse.py")
    for f in FILES:
        _patch("patch_voice_barge.py", d / f)
    return d


@pytest.fixture()
def control(work):
    spec = importlib.util.spec_from_file_location("cc_barge", work / "control_channel.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cc_barge"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_not_speaking_by_default(control):
    assert control.ControlState().is_speaking is False


def test_barge_in_cuts_only_while_speaking(control):
    state = control.ControlState()
    state.set_speaking(True)
    assert state.request_barge_in() is True
    assert state.snapshot().is_skip_forward


def test_barge_in_after_playback_cannot_end_the_listen_turn(control):
    state = control.ControlState()
    state.set_speaking(True)
    state.set_speaking(False)
    assert state.request_barge_in() is False
    assert not state.snapshot().is_skip_forward


def test_stop_dominates_barge_in(control):
    state = control.ControlState()
    state.set_speaking(True)
    state.request_stop()
    assert state.request_barge_in() is False
    assert state.snapshot().is_stopped


def test_barge_in_is_a_valid_wire_command(control):
    state = control.ControlState()
    state.set_speaking(True)
    control.parse_command('{"command": "barge_in"}').apply_to(state)
    assert state.snapshot().is_skip_forward


def test_status_reports_speaking(work):
    src = (work / "control_socket.py").read_text()
    assert '"speaking": control_state.is_speaking,' in src


@pytest.mark.parametrize("fname,func", [
    ("core.py", "_wait_for_player_with_control"),
    ("converse.py", "_play_samples_controllable"),
])
def test_playback_is_bracketed_by_speaking(work, fname, func):
    tree = ast.parse((work / fname).read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == func)
    src = ast.unparse(fn)
    assert "set_speaking(True)" in src
    assert "finally" in src and "set_speaking(False)" in src
    assert any(isinstance(n, ast.AsyncFunctionDef) and n.name == f"{func}_inner" for n in ast.walk(tree))


def test_patch_is_idempotent(work):
    for f in FILES:
        before = (work / f).read_text()
        assert "already patched" in _patch("patch_voice_barge.py", work / f)
        assert (work / f).read_text() == before


def test_anchor_drift_fails_loudly(tmp_path):
    target = tmp_path / "core.py"
    target.write_text("# nothing to anchor on\n")
    r = subprocess.run([sys.executable, str(PATCHES / "patch_voice_barge.py"), str(target)],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "ANCHOR DRIFT" in r.stderr


def test_barge_flag_is_consumed_once(control):
    state = control.ControlState()
    state.set_speaking(True)
    state.request_barge_in()
    assert state.take_barged() is True
    assert state.take_barged() is False


def test_refused_barge_sets_no_flag(control):
    state = control.ControlState()
    state.request_barge_in()
    assert state.take_barged() is False


def test_barge_flag_survives_the_skip_forward_consume(control):
    state = control.ControlState()
    state.set_speaking(True)
    state.request_barge_in()
    state.reset(preserve_hold=True)
    assert state.take_barged() is True


def test_listen_start_is_reported_and_cleared_by_the_next_barge(control):
    state = control.ControlState()
    state.mark_listen_started(123.0)
    assert state.listen_started_at == 123.0
    state.set_speaking(True)
    state.request_barge_in()
    assert state.listen_started_at is None


def test_status_reports_listen_start(work):
    assert '"listen_started_at": control_state.listen_started_at,' in (work / "control_socket.py").read_text()


def _listen_src(work):
    tree = ast.parse((work / "converse.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "listen_and_transcribe")
    return ast.unparse(fn)


def test_voice_cut_skips_pause_and_chime(work):
    src = _listen_src(work)
    assert "take_barged()" in src
    assert "pre_listen_pause = 0" in src and "chime_enabled = False" in src
    assert src.index("take_barged()") < src.index("play_audio_feedback")


def test_voice_cut_merges_preroll_after_recording(work):
    src = _listen_src(work)
    assert "prepend_preroll" in src
    assert src.index("record_audio_with_silence_detection") < src.index("prepend_preroll") < src.index("RECORDING_END")


def test_recording_treats_a_barge_as_speech_in_progress(work):
    tree = ast.parse((work / "converse.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "record_audio_with_silence_detection")
    src = ast.unparse(fn)
    assert "mark_listen_started" in src and "_voice_barge_pending" in src
