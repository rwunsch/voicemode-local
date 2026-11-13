"""The env port override: which variable belongs to which service kind."""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_supervisor_profile import make_profile  # noqa: E402

from vml_supervisor.ports import requested_port  # noqa: E402
from vml_supervisor.profile import PortOverride  # noqa: E402


def _with(keys):
    return dataclasses.replace(make_profile(), port_override=PortOverride("env", None, keys))


def test_positional_keys_follow_the_service_kinds_order(monkeypatch):
    monkeypatch.setenv("T", "7001")
    monkeypatch.setenv("S", "7002")
    profile = _with(("T", "S"))
    assert requested_port(profile, "tts") == (7001, "T") and requested_port(profile, "stt") == (7002, "S")


def test_keys_can_name_their_kind_and_then_order_does_not_matter(monkeypatch):
    monkeypatch.setenv("T", "7001")
    monkeypatch.setenv("S", "7002")
    profile = _with(("stt=S", "tts=T"))
    assert requested_port(profile, "tts") == (7001, "T") and requested_port(profile, "stt") == (7002, "S")


def test_a_named_key_for_one_kind_leaves_the_other_without_an_override(monkeypatch):
    monkeypatch.setenv("S", "7002")
    monkeypatch.setenv("X", "7003")
    profile = _with(("stt=S",))
    assert requested_port(profile, "tts") == (None, "")
    assert requested_port(profile, "stt") == (7002, "S")
