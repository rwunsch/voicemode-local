"""Behavioural tests for the PTT hold patches (patch_control_hold + patch_converse_hold).

`control_channel.py` is stdlib-only, so the patched module is loaded directly
from a scratch copy — no venv, no voice_mode package import, no audio.

Run:
    VM812_SRC=<dir with pristine 8.12.0 sources> pytest tests/test_ptt_hold_patch.py
`VM812_SRC` must contain pristine `control_channel.py` and `converse.py`.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"


@pytest.fixture(scope="module")
def src_dir() -> Path:
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    p = Path(raw)
    if not (p / "control_channel.py").exists():
        pytest.fail(f"VM812_SRC={p} has no control_channel.py")
    return p


@pytest.fixture(scope="module")
def control(tmp_path_factory, src_dir):
    """Patch a scratch copy of control_channel.py and import it."""
    work = tmp_path_factory.mktemp("ptt")
    target = work / "control_channel.py"
    shutil.copy(src_dir / "control_channel.py", target)

    r = subprocess.run(
        [sys.executable, str(PATCHES / "patch_control_hold.py"), str(target)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"patch failed: {r.stderr}"

    spec = importlib.util.spec_from_file_location("vml_control_channel", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- control_channel: the hold primitive ------------------------------------

def test_hold_commands_are_in_the_allowlist(control):
    assert control.COMMAND_HOLD_START == "hold_start"
    assert control.COMMAND_HOLD_END == "hold_end"
    assert control.COMMAND_HOLD_START in control.VALID_COMMANDS
    assert control.COMMAND_HOLD_END in control.VALID_COMMANDS


def test_parse_command_accepts_hold(control):
    cmd = control.parse_command('{"command": "hold_start"}')
    assert cmd.command == "hold_start"
    cmd = control.parse_command('{"command": "hold_end"}')
    assert cmd.command == "hold_end"


def test_parse_command_still_rejects_unknown(control):
    """The allowlist must not have become permissive."""
    with pytest.raises(control.ControlCommandError):
        control.parse_command('{"command": "hold_sideways"}')


def test_snapshot_defaults_to_not_holding(control):
    st = control.ControlState()
    assert st.snapshot().is_holding is False


def test_hold_start_then_end(control):
    st = control.ControlState()
    assert st.request_hold_start() is True
    assert st.snapshot().is_holding is True
    assert st.request_hold_end() is True
    assert st.snapshot().is_holding is False


def test_hold_start_is_idempotent_for_key_repeat(control):
    """Auto-repeat on a held key must not corrupt the flag."""
    st = control.ControlState()
    assert st.request_hold_start() is True
    assert st.request_hold_start() is True
    assert st.snapshot().is_holding is True


def test_stray_hold_end_is_refused(control):
    """A key-up with no hold in progress must not report success."""
    st = control.ControlState()
    assert st.request_hold_end() is False
    assert st.snapshot().is_holding is False


def test_stop_dominates_hold(control):
    """stop is the harder terminal, exactly as it dominates skip_forward."""
    st = control.ControlState()
    st.request_stop()
    assert st.request_hold_start() is False
    assert st.snapshot().is_holding is False


def test_hold_is_orthogonal_to_state(control):
    """Holding must not disturb the play/hold/cut state machine."""
    st = control.ControlState()
    st.request_hold_start()
    assert st.snapshot().is_running is True
    assert st.snapshot().is_holding is True
    st.request_skip_forward()
    snap = st.snapshot()
    assert snap.is_skip_forward is True
    assert snap.is_holding is True  # independent axis


def test_reset_clears_the_hold(control):
    """A hold must not leak into the next utterance."""
    st = control.ControlState()
    st.request_hold_start()
    st.reset()
    assert st.snapshot().is_holding is False


def test_wait_while_paused_snapshot_carries_hold(control):
    """The second ControlSnapshot construction site must carry the flag too."""
    st = control.ControlState()
    st.request_hold_start()
    snap = st.wait_while_paused(timeout=0.01)
    assert snap.is_holding is True


# --- converse.py: the consumer ----------------------------------------------

@pytest.fixture(scope="module")
def patched_converse(tmp_path_factory, src_dir) -> str:
    work = tmp_path_factory.mktemp("ptt_converse")
    target = work / "converse.py"
    shutil.copy(src_dir / "converse.py", target)
    r = subprocess.run(
        [sys.executable, str(PATCHES / "patch_converse_hold.py"), str(target)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"patch failed: {r.stderr}"
    return target.read_text()


def test_silence_exit_is_guarded_by_hold(patched_converse):
    assert 'not getattr(snap, "is_holding", False)' in patched_converse
    assert "and silence_duration_ms >= SILENCE_THRESHOLD_MS):" in patched_converse


def test_release_breaks_the_recording_loop(patched_converse):
    assert "_ptt_was_holding = True" in patched_converse
    assert "elif _ptt_was_holding:" in patched_converse
    assert "PTT hold released" in patched_converse


def test_tracker_is_initialised_before_the_loop(patched_converse):
    """_ptt_was_holding must be bound before first use or the loop NameErrors."""
    init = patched_converse.index("_ptt_was_holding = False")
    use = patched_converse.index("elif _ptt_was_holding:")
    assert init < use, "tracker initialised after its first use"


def test_stall_backstop_and_max_duration_survive(patched_converse):
    """Holding suppresses silence detection ONLY -- the other bounds remain."""
    assert "AUDIO_STALL_TIMEOUT" in patched_converse
    assert "max_duration" in patched_converse


# --- the wiring, not just the primitive ------------------------------------
#
# These exist because the first version of this patch passed every test above
# and was still completely inert in production: the socket listener dispatches
# via ControlCommand.apply_to(), which knew nothing about the hold commands.
# Testing ControlState directly never touched that path.

def test_apply_to_drives_hold_start(control):
    st = control.ControlState()
    control.parse_command('{"command": "hold_start"}').apply_to(st)
    assert st.snapshot().is_holding is True


def test_apply_to_drives_hold_end(control):
    st = control.ControlState()
    st.request_hold_start()
    control.parse_command('{"command": "hold_end"}').apply_to(st)
    assert st.snapshot().is_holding is False


def test_apply_to_handles_every_valid_command(control):
    """No command may parse successfully and then do nothing on apply.

    apply_to raises ControlCommandError on an unhandled command, so this fails
    loudly for any future command added to VALID_COMMANDS without a dispatch arm.
    """
    st = control.ControlState()
    for cmd in control.VALID_COMMANDS:
        parsed = control.parse_command('{"command": "%s"}' % cmd)
        parsed.apply_to(st)   # must not raise
        st.reset()


def test_reset_preserves_hold_when_asked(control):
    """The skip_forward edge-consume must not wipe the hold that press was for.

    A PTT press during playback sends skip_forward (barge-in) then hold_start;
    converse then consumes the skip_forward edge with reset(). Without
    preserve_hold that reset wiped the hold, so the mic opened and closed again
    on the first second of silence -- the exact live failure.
    """
    st = control.ControlState()
    st.request_hold_start()
    st.reset(preserve_hold=True)
    assert st.snapshot().is_holding is True


def test_reset_clears_hold_by_default(control):
    """Turn-boundary reset must still clear it, so a hold can't leak across turns."""
    st = control.ControlState()
    st.request_hold_start()
    st.reset()
    assert st.snapshot().is_holding is False


def test_reset_still_clears_everything_else_when_preserving(control):
    st = control.ControlState()
    st.request_skip_forward()
    st.request_hold_start()
    st.reset(preserve_hold=True)
    snap = st.snapshot()
    assert snap.is_skip_forward is False, "skip_forward edge must still be consumed"
    assert snap.is_running is True
    assert snap.is_holding is True
