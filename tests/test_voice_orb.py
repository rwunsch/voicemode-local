"""Run the orb against pristine VoiceMode code, without recording or playing audio."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
PATCHES = ROOT / "patches"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def control(tmp_path, monkeypatch):
    src = os.getenv("VM812_SRC")
    if not src:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    target = tmp_path / "control_channel.py"
    shutil.copyfile(Path(src) / target.name, target)
    for name in ("patch_voice_barge", "patch_voice_pill", "patch_voice_orb"):
        patch = _load(PATCHES / f"{name}.py", name)
        assert patch.apply(target) == 0
    before = target.read_bytes()
    assert patch.apply(target) == 0
    assert target.read_bytes() == before
    return _load(target, "voice_orb_control")


def test_levels_follow_real_sample_values_and_expire(control, monkeypatch):
    now = [10.0]
    # Replace only the patched module's view of the monotonic clock.
    from types import SimpleNamespace
    monkeypatch.setattr(control, "time", SimpleNamespace(monotonic=lambda: now[0],
                        monotonic_ns=lambda: int(now[0] * 1000000000)))
    state = control.ControlState()
    state.set_listening(True)
    state.pill_level("listening", np.full(512, -16384, dtype=np.int16), 32768)
    assert state.pill_levels() == {"listening": 0.5}
    now[0] += .10
    state.pill_level("listening", np.zeros(512, dtype=np.int16), 32768)
    assert state.pill_levels() == {"listening": 0.0}
    state.set_listening(False)
    assert state.pill_levels() == {}
    state.set_speaking(True)
    state.pill_level("speaking", np.full(512, 0.25, dtype=np.float32))
    assert state.pill_levels() == {"speaking": 0.25}
    now[0] += .36
    assert state.pill_levels() == {}
    state.pill_level("speaking", np.array([np.nan], dtype=np.float32))
    assert state.pill_levels() == {}
    state.pill_level("speaking", np.ones(512, dtype=np.float32))
    state.request_pause()
    assert state.pill_levels() == {}
    state.request_resume()
    assert state.pill_levels() == {}


def test_twice_interrupt_then_listen_and_ignore_old_click(control):
    state = control.ControlState()
    previous = None
    for _ in range(2):
        state.set_speaking(True)
        playback_id = state.pill_playback_id
        assert playback_id
        if previous:
            control.parse_command(json.dumps({"command": "pill_interrupt", "message": previous})).apply_to(state)
            assert not state.snapshot().is_skip_forward
        click = control.parse_command(json.dumps({"command": "pill_interrupt", "message": playback_id}))
        click.apply_to(state)
        assert state.snapshot().is_skip_forward
        assert state.pill_playback_id is None
        state.set_speaking(False)
        state.reset()  # existing converse path consumes skip_forward before recording
        state.set_listening(True)
        click.apply_to(state)
        assert not state.snapshot().is_skip_forward
        assert state.is_listening
        state.set_listening(False)
        previous = playback_id
