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


# --- wake an idle session on a hold with no voice turn (2026-11-07) ----------

class FakeClient:
    def __init__(self, outcome):
        self.outcome, self.sent, self.woke = outcome, [], 0

    def action_outcome(self, action):
        self.sent.append(action)
        return self.outcome

    def wake_idle_session(self):
        self.woke += 1
        return True, "sent"


@pytest.mark.parametrize("action,outcome,woke", [
    ("hold_release", "no-channel", 1),
    ("hold_release", "sent", 0),
    ("hold_start", "no-channel", 0),
    ("short_press", "no-channel", 0),
])
def test_relay_wakes_only_on_a_hold_with_no_channel(relay, action, outcome, woke):
    client = FakeClient(outcome)
    relay.forward(action, client)
    assert client.sent == [action] and client.woke == woke


def test_relay_forward_never_raises(relay):
    class Broken(FakeClient):
        def wake_idle_session(self):
            raise RuntimeError("x")
    relay.forward("hold_release", Broken("no-channel"))


def test_relay_does_not_send_hold_end_for_a_hold_that_started_with_no_channel(relay):
    client = FakeClient("no-channel")
    relay.forward("hold_start", client)
    relay.forward("hold_release", client)
    assert client.sent == ["hold_start"] and client.woke == 1


def test_relay_still_sends_hold_end_after_a_hold_with_a_channel(relay):
    client = FakeClient("sent")
    relay.forward("hold_start", client)
    relay.forward("hold_release", client)
    assert client.sent == ["hold_start", "hold_release"] and client.woke == 0
