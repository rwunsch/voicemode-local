"""Tests for patches/ptt_relay.py — the WSL2 Windows->control-socket hop."""
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def relay():
    spec = importlib.util.spec_from_file_location(
        "vml_ptt_relay", REPO / "patches" / "ptt_relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("line,expected", [
    ('{"action": "hold_start"}', "hold_start"),
    ('{"type": "hold_release"}', "hold_release"),       # old ptt_ipc field name
    ("short_press", "short_press"),                      # bare name
    ("  hold_start  \n", "hold_start"),                  # whitespace tolerated
    ('{"action": "press"}', "press"),
])
def test_valid_actions_parse(relay, line, expected):
    assert relay.parse_action(line) == expected


@pytest.mark.parametrize("line", [
    "", "   ", "\n",
    "hold_sideways",                                     # not in the allowlist
    '{"action": "skip_forward"}',                        # a control command, not a PTT action
    '{"action": "rm -rf /"}',
    "{not json",
    '["hold_start"]',                                    # not an object
    '{"action": 3}',
    "x" * 2000,                                          # over MAX_LINE
])
def test_invalid_input_is_rejected(relay, line):
    assert relay.parse_action(line) is None


def test_allowlist_matches_ptt_core(relay):
    """The relay must accept exactly the actions ptt_core can emit."""
    spec = importlib.util.spec_from_file_location(
        "vml_ptt_core", REPO / "patches" / "ptt_core.py")
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    assert relay.VALID_ACTIONS == {a.value for a in core.PTTAction}


def test_binds_loopback_by_default(relay):
    """Nothing off-box drives the microphone unless explicitly opted in."""
    import inspect
    src = inspect.getsource(relay.serve)
    assert '"VOICEMODE_PTT_HOST", "127.0.0.1"' in src, \
        "the default bind must stay loopback"
