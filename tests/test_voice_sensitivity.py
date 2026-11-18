"""patches/voice_sensitivity.py: the live override of how early voice mode ends a turn
(VAD aggressiveness and silence length), and the voice_sensitivity MCP tool
(patches/voice_barge_tool.py) that sets it.

Robert, 2026-11-18: with background noise the turn never ended at aggressiveness 2 and
2000 ms of silence; he had to mute. The override lives in <base dir>/sensitivity.json and
is read at each recording, so a change applies at the next listen, no restart.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name="vs_under_test"):
    spec = importlib.util.spec_from_file_location(name, PATCHES / "voice_sensitivity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def vs(monkeypatch, tmp_path):
    """The module under a throwaway HOME (never the real ~/.voicemode)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("VOICEMODE_BASE_DIR", raising=False)
    return _load()


def _file(vs):
    return vs.store_path()


def _write_raw(vs, text, mtime=None):
    path = _file(vs)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, ns=(mtime, mtime))
    return path


# -- where it lives ------------------------------------------------------------

def test_file_is_in_the_base_dir(vs, tmp_path):
    assert _file(vs) == tmp_path / ".voicemode" / "sensitivity.json"


def test_base_dir_setting_is_followed(vs, tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "rt"))
    assert _file(vs) == tmp_path / "rt" / "sensitivity.json"


# -- resolution order: explicit argument > override file > config ---------------

def test_no_file_gives_the_config_values(vs):
    assert vs.resolve(None, 2, 2000) == (2, 2000)
    assert not _file(vs).exists()


def test_override_beats_config(vs):
    vs.write_override(3, 1200, "earlier")
    assert vs.resolve(None, 2, 2000) == (3, 1200)


def test_explicit_argument_beats_override(vs):
    vs.write_override(3, 1200, None)
    assert vs.resolve(0, 2, 2000) == (0, 1200)


def test_a_field_left_unset_falls_through_to_config(vs):
    vs.write_override(None, 900, None)
    assert vs.resolve(None, 2, 2000) == (2, 900)
    vs.write_override(1, None, None)
    assert vs.resolve(None, 2, 2000) == (1, 2000)


@pytest.mark.parametrize("text", [
    "", "not json", "[1, 2]", "{\"vad_aggressiveness\": 7, \"silence_ms\": 99999}",
    "{\"vad_aggressiveness\": true, \"silence_ms\": \"1200\"}",
    "{\"vad_aggressiveness\": -1, \"silence_ms\": 100}", "{\"vad_aggressiveness\": 2.5}",
])
def test_corrupt_or_out_of_range_file_is_no_override(vs, text):
    _write_raw(vs, text)
    assert vs.read_override() is None
    assert vs.resolve(None, 2, 2000) == (2, 2000)


def test_one_bad_field_does_not_drop_the_good_one(vs):
    _write_raw(vs, json.dumps({"vad_aggressiveness": 9, "silence_ms": 1500}))
    assert vs.resolve(None, 2, 2000) == (2, 1500)


def test_resolve_never_raises(vs, monkeypatch):
    def boom():
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(vs, "read_override", boom)
    assert vs.resolve(None, 2, 2000) == (2, 2000)
    assert vs.resolve(1, 2, 2000) == (1, 2000)


def test_unreadable_base_dir_is_no_override(vs, monkeypatch):
    monkeypatch.setattr(vs, "store_path", lambda: (_ for _ in ()).throw(OSError("no home")))
    assert vs.read_override() is None
    assert vs.resolve(None, 2, 2000) == (2, 2000)


# -- re-read when the file changes (mtime-cached) ---------------------------------

def test_cached_while_unchanged_and_reread_on_mtime_change(vs):
    old = 1_700_000_000_000_000_000
    a = json.dumps({"vad_aggressiveness": 3, "silence_ms": 1200, "preset": None})
    b = json.dumps({"vad_aggressiveness": 1, "silence_ms": 2500, "preset": None})
    assert len(a) == len(b)
    _write_raw(vs, a, mtime=old)
    assert vs.resolve(None, 2, 2000) == (3, 1200)
    # Same size, same stamp: the cache answers (the file is not parsed again).
    _write_raw(vs, b, mtime=old)
    assert vs.resolve(None, 2, 2000) == (3, 1200)
    # A new stamp: read again at the next recording.
    _write_raw(vs, b, mtime=old + 5_000_000_000)
    assert vs.resolve(None, 2, 2000) == (1, 2500)


def test_a_removed_file_is_seen_at_once(vs):
    vs.write_override(3, 800, "noisy")
    assert vs.resolve(None, 2, 2000) == (3, 800)
    vs.clear_override()
    assert vs.resolve(None, 2, 2000) == (2, 2000)


def test_a_change_through_write_override_is_seen_at_once(vs):
    vs.write_override(3, 1200, "earlier")
    assert vs.resolve(None, 2, 2000) == (3, 1200)
    vs.write_override(1, 2500, "later")   # same size, same coarse second: still seen
    assert vs.resolve(None, 2, 2000) == (1, 2500)


# -- writing ----------------------------------------------------------------------

def test_write_is_atomic_json_and_leaves_no_temp_files(vs):
    vs.write_override(3, 1200, "earlier")
    path = _file(vs)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "vad_aggressiveness": 3, "silence_ms": 1200, "preset": "earlier"}
    assert [p.name for p in path.parent.iterdir()] == ["sensitivity.json"]


def test_clear_is_idempotent(vs):
    assert vs.clear_override() is False
    vs.write_override(3, 1200, None)
    assert vs.clear_override() is True
    assert vs.clear_override() is False


# -- the preset table ---------------------------------------------------------------

def test_preset_table():
    vs = _load("vs_presets")
    assert {k: (v[0], v[1]) for k, v in vs.PRESETS.items()} == {
        "earlier": (3, 1200), "noisy": (3, 800), "later": (1, 2500)}
    for vad, ms, meaning in vs.PRESETS.values():
        assert vs.VAD_MIN <= vad <= vs.VAD_MAX and vs.SILENCE_MIN_MS <= ms <= vs.SILENCE_MAX_MS
        assert meaning


def test_preset_table_is_in_the_doc():
    vs = _load("vs_doc")
    doc = (PATCHES.parent / "docs" / "voice-sensitivity" / "README.md").read_text(encoding="utf-8")
    for name, (vad, ms, _) in vs.PRESETS.items():
        assert f"| `{name}` | {vad} | {ms} |" in doc, name


# -- control(): the tool's words --------------------------------------------------

CFG = (2, 2000)


def test_status_without_override(vs):
    out = vs.control("status", configured=CFG)
    lines = out.splitlines()
    assert 2 <= len(lines) <= 4
    assert "Configured: aggressiveness 2, silence 2000 ms" in out
    assert "No override" in out
    assert "aggressiveness 2" in lines[-1] and "2000 ms" in lines[-1]


def test_status_with_override_names_it_and_the_effective_values(vs):
    vs.write_override(3, 1200, "earlier")
    out = vs.control("status", configured=CFG)
    lines = out.splitlines()
    assert 2 <= len(lines) <= 4
    assert "Configured: aggressiveness 2, silence 2000 ms" in out
    assert "earlier" in out
    assert "In effect" in lines[-1] and "aggressiveness 3" in lines[-1] and "1200 ms" in lines[-1]
    assert "background noise" in out   # what the number means


def test_set_preset_writes_and_says_when_it_applies(vs):
    out = vs.control("set", level="earlier", configured=CFG)
    assert vs.read_override() == {"vad_aggressiveness": 3, "silence_ms": 1200, "preset": "earlier"}
    assert "aggressiveness 3" in out and "1200 ms" in out and "next listen" in out
    assert "reset" in out


def test_set_is_idempotent(vs):
    first = vs.control("set", level="noisy", configured=CFG)
    assert vs.control("set", level="noisy", configured=CFG) == first
    assert vs.read_override() == {"vad_aggressiveness": 3, "silence_ms": 800, "preset": "noisy"}


def test_set_level_is_case_and_space_tolerant(vs):
    vs.control("  SET ", level=" Later ", configured=CFG)
    assert vs.read_override()["preset"] == "later"


def test_set_explicit_numbers(vs):
    out = vs.control("set", vad_aggressiveness=3, silence_ms=1500, configured=CFG)
    assert vs.read_override() == {"vad_aggressiveness": 3, "silence_ms": 1500, "preset": None}
    assert "1500 ms" in out


def test_set_numbers_given_as_strings(vs):
    vs.control("set", vad_aggressiveness="3", silence_ms="1500", configured=CFG)
    assert vs.read_override()["silence_ms"] == 1500


def test_set_one_number_keeps_the_other_from_the_override(vs):
    vs.control("set", level="earlier", configured=CFG)
    vs.control("set", silence_ms=1000, configured=CFG)
    assert vs.read_override() == {"vad_aggressiveness": 3, "silence_ms": 1000, "preset": None}


def test_set_level_with_a_number_overrides_that_field(vs):
    vs.control("set", level="earlier", silence_ms=1500, configured=CFG)
    assert vs.read_override() == {"vad_aggressiveness": 3, "silence_ms": 1500, "preset": None}


@pytest.mark.parametrize("kw,needle", [
    ({"vad_aggressiveness": 4}, "0 to 3"), ({"vad_aggressiveness": -1}, "0 to 3"),
    ({"vad_aggressiveness": True}, "0 to 3"), ({"vad_aggressiveness": 2.5}, "0 to 3"),
    ({"vad_aggressiveness": "high"}, "0 to 3"),
    ({"silence_ms": 299}, "300 to 5000"), ({"silence_ms": 5001}, "300 to 5000"),
    ({"silence_ms": "soon"}, "300 to 5000"),
    ({"level": "faster"}, "earlier"), ({}, "Nothing to set"),
])
def test_set_refuses_bad_input_and_writes_nothing(vs, kw, needle):
    vs.control("set", level="later", configured=CFG)
    before = _file(vs).read_bytes()
    out = vs.control("set", configured=CFG, **kw)
    assert needle in out and "not changed" in out.lower()
    assert _file(vs).read_bytes() == before


def test_set_refusal_with_no_file_writes_none(vs):
    vs.control("set", vad_aggressiveness=9, configured=CFG)
    assert not _file(vs).exists()


def test_reset_removes_and_is_idempotent(vs):
    vs.control("set", level="earlier", configured=CFG)
    out = vs.control("reset", configured=CFG)
    assert not _file(vs).exists()
    assert "removed" in out.lower() and "aggressiveness 2" in out and "2000 ms" in out
    again = vs.control("reset", configured=CFG)
    assert "no override" in again.lower() and "aggressiveness 2" in again


@pytest.mark.parametrize("action", ["louder", "", None])
def test_unknown_action_lists_the_valid_ones(vs, action):
    out = vs.control(action, configured=CFG)
    assert "status" in out and "set" in out and "reset" in out
    assert not _file(vs).exists()


def test_a_write_failure_is_words_not_a_traceback(vs, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(vs, "write_override", boom)
    out = vs.control("set", level="earlier", configured=CFG)
    assert "read-only" in out and "nothing changed" in out.lower()


def test_configured_values_from_config_module_else_upstream_defaults(vs, monkeypatch):
    cfg = types.SimpleNamespace(VAD_AGGRESSIVENESS=2, SILENCE_THRESHOLD_MS=2000)
    assert vs.configured_values(cfg) == (2, 2000)
    monkeypatch.delenv("VOICEMODE_VAD_AGGRESSIVENESS", raising=False)
    monkeypatch.delenv("VOICEMODE_SILENCE_THRESHOLD_MS", raising=False)
    assert vs.configured_values(object()) == (3, 1000)   # config.py:948-949 defaults
    monkeypatch.setenv("VOICEMODE_VAD_AGGRESSIVENESS", "1")
    monkeypatch.setenv("VOICEMODE_SILENCE_THRESHOLD_MS", "1700")
    assert vs.configured_values(object()) == (1, 1700)


def test_stdlib_only_and_no_provider_key_text():
    src = (PATCHES / "voice_sensitivity.py").read_text(encoding="utf-8")
    for key in ("OPENAI_API_KEY", "CARTESIA_API_KEY"):
        assert key not in src
    import ast
    tree = ast.parse(src)
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    # voice_mode is the host package (its config, imported lazily inside the server only).
    assert mods <= set(sys.stdlib_module_names) | {"__future__", "voice_mode"}, mods
    top = {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    top |= {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in top if m and m.startswith("voice_mode")]


# -- the MCP tool ---------------------------------------------------------------------

@pytest.fixture()
def tool(monkeypatch, tmp_path):
    """voice_barge_tool.py loaded against fakes; voice_sensitivity is the real module."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("VOICEMODE_BASE_DIR", raising=False)
    sens = _load("vs_for_tool")
    tools = []
    server = types.ModuleType("voice_mode.server")
    server.mcp = types.SimpleNamespace(tool=lambda: (lambda f: tools.append(f) or f))
    barge = types.ModuleType("voice_mode.voice_barge")
    barge.barge_control = lambda a: a
    config = types.ModuleType("voice_mode.config")
    config.VAD_AGGRESSIVENESS, config.SILENCE_THRESHOLD_MS = 2, 2000
    pkg = types.ModuleType("voice_mode")
    pkg.voice_pill_state = types.ModuleType("voice_mode.voice_pill_state")
    pkg.pill_autostart = types.ModuleType("voice_mode.pill_autostart")
    pkg.voice_sensitivity, pkg.config = sens, config
    for name, mod in (("voice_mode", pkg), ("voice_mode.server", server),
                      ("voice_mode.voice_barge", barge),
                      ("voice_mode.voice_pill_state", pkg.voice_pill_state),
                      ("voice_mode.pill_autostart", pkg.pill_autostart),
                      ("voice_mode.voice_sensitivity", sens), ("voice_mode.config", config)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("vbt_sens", PATCHES / "voice_barge_tool.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, sens, tools


def test_tool_is_registered_and_says_when_to_use_it(tool):
    mod, _, tools = tool
    assert mod.voice_sensitivity in tools
    doc = mod.voice_sensitivity.__doc__
    for words in ("too late", "too early", "background noise", "earlier", "later", "noisy", "reset"):
        assert words in doc, words


def test_tool_signature(tool):
    import inspect
    mod, _, _ = tool
    params = inspect.signature(mod.voice_sensitivity).parameters
    assert list(params) == ["action", "level", "vad_aggressiveness", "silence_ms"]
    assert params["action"].default == "status"
    assert all(params[p].default is None for p in ("level", "vad_aggressiveness", "silence_ms"))


def test_tool_status_set_reset_use_the_config_values(tool):
    mod, sens, _ = tool
    run = lambda *a, **k: asyncio.run(mod.voice_sensitivity(*a, **k))
    assert "Configured: aggressiveness 2, silence 2000 ms" in run()
    out = run("set", level="earlier")
    assert "1200 ms" in out and sens.read_override()["preset"] == "earlier"
    assert "aggressiveness 3" in run("status").splitlines()[-1]
    assert "removed" in run("reset").lower() and sens.read_override() is None


def test_tool_answers_in_words_when_the_module_is_missing(tool, monkeypatch):
    mod, _, _ = tool
    monkeypatch.setitem(sys.modules, "voice_mode.voice_sensitivity", None)
    out = asyncio.run(mod.voice_sensitivity("status"))
    assert isinstance(out, str) and "not installed" in out


def test_other_tools_still_load_without_the_sensitivity_module(tool, monkeypatch):
    """voice_barge_tool.py must not import voice_sensitivity at load time: a harness that
    has not copied the module in still gets voice_barge, voice_stop and voice_pill."""
    import ast
    tree = ast.parse((PATCHES / "voice_barge_tool.py").read_text(encoding="utf-8"))
    top_imports = [ast.dump(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert top_imports and not [d for d in top_imports if "voice_sensitivity" in d]
