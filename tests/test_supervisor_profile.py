"""Tests for vml_supervisor: HostProfile, Result, package hygiene."""
import ast
import dataclasses
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

PKG = REPO / "vml_supervisor"


def make_profile(**over):
    from vml_supervisor.profile import (
        HelperSpec, HostProfile, PortOverride, ServiceSpec,
    )
    base = dict(
        name="test",
        root=Path("/r"),
        run_dir=Path("/r/run"),
        runtime_dir=Path("/r/runtime"),
        python=Path("/r/.venv/bin/python"),
        working_dir=Path("/r"),
        env={"A": "1"},
        services={
            "tts": ServiceSpec(["python", "tts.py", "--port", "{port}"], "tts.py",
                               (8890, 8899), "/health", "kokoro"),
            "stt": ServiceSpec(["python", "stt.py"], "stt.py",
                               (9010, 9019), "/", "whisper"),
        },
        helpers={
            "barge": HelperSpec(["python", "barge.py"], "barge.py", "barge.log",
                                ("linux", "darwin"), True),
            "ptt": HelperSpec(["python", "ptt.py"], "ptt.py", "ptt.log",
                              ("win32",), False),
        },
        reserved_ports=(1234,),
        port_override=PortOverride("env", Path("/r/ports.env"), ("TTS_PORT", "STT_PORT")),
        start_timeout=30.0,
        stop_timeout=10.0,
        watch_interval=5.0,
        restart_limit=(3, 60.0),
        lease_markers=("lease-a", "lease-b"),
        voice_server_marker="voice-server",
        host_command=["python", "host.py"],
        host_marker="host.py",
        watch_command=["python", "watch.py"],
        watch_marker="watch.py",
        job_name="vml-job",
        host_start_grace=60.0,
        hooks={"read_mode": "mymod:read_mode"},
        hook_paths=(Path("/r/hooks"),),
    )
    base.update(over)
    return HostProfile(**base)


def test_a_profile_round_trips_through_its_file(tmp_path):
    from vml_supervisor.profile import HostProfile
    p = make_profile()
    f = tmp_path / "profile.json"
    p.to_file(f)
    assert json.loads(f.read_text())["root"] == "/r"
    assert HostProfile.from_file(f) == p
    assert not list(tmp_path.glob("*.tmp*")) and [x.name for x in tmp_path.iterdir()] == ["profile.json"]


def test_profile_is_frozen():
    p = make_profile()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.name = "x"


def test_result_defaults_and_results_list():
    from vml_supervisor.result import Result, Results
    r = Result("ok")
    assert (r.code, r.detail, r.data) == ("ok", "", {})
    assert isinstance(Results(), list)


def test_valid_profile_has_no_results():
    assert make_profile().validate() == []


@pytest.mark.parametrize("port", [8880, 8881, 8882, 2022, 9000, 1234])
def test_validate_rejects_reserved_and_overlapping_ranges(port):
    from vml_supervisor.profile import ServiceSpec
    p = make_profile()
    tts = ServiceSpec(["x"], "x", (port - 2, port + 2), "/", "kokoro")
    bad = make_profile(services={"tts": tts, "stt": p.services["stt"]})
    assert "port_range_reserved" in [r.code for r in bad.validate()]


def test_validate_flags_overlapping_ranges():
    from vml_supervisor.profile import ServiceSpec
    p = make_profile()
    stt = ServiceSpec(["x"], "x", (8895, 8905), "/", "whisper")
    bad = make_profile(services={"tts": p.services["tts"], "stt": stt})
    assert "port_ranges_overlap" in [r.code for r in bad.validate()]


def test_the_package_names_no_provider_key():
    needles = ["OPENAI" + "_API_KEY", "CARTESIA" + "_API_KEY"]
    files = list(PKG.rglob("*"))
    assert files
    for f in files:
        if f.is_file() and f.suffix in (".py", ".json", ".md", ".txt"):
            text = f.read_text()
            for n in needles:
                assert n not in text, f"{n} in {f}"


def _guarded_import_nodes(tree):
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
            isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError")
            or isinstance(h.type, ast.Tuple)
            for h in node.handlers
        ):
            for sub in node.body:
                for n in ast.walk(sub):
                    guarded.add(id(n))
    return guarded


def test_the_package_imports_only_the_stdlib():
    stdlib = set(sys.stdlib_module_names)
    for f in PKG.rglob("*.py"):
        tree = ast.parse(f.read_text())
        guarded = _guarded_import_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                if n == "vml_supervisor" or n in stdlib:
                    continue
                assert n == "psutil" and id(node) in guarded, f"{f}: non-stdlib import {n}"


def test_profile_round_trip_keeps_hooks_and_version(tmp_path):
    from vml_supervisor.profile import HostProfile
    p = make_profile(schema_version=1)
    f = tmp_path / "p.json"
    p.to_file(f)
    q = HostProfile.from_file(f)
    assert q.hooks == {"read_mode": "mymod:read_mode"}
    assert q.hook_paths == (Path("/r/hooks"),)
    assert q.schema_version == 1
    assert not hasattr(q, "mode_source")


def test_a_newer_schema_is_refused_with_a_result(tmp_path):
    from vml_supervisor.profile import load_profile
    p = make_profile()
    f = tmp_path / "p.json"
    p.to_file(f)
    d = json.loads(f.read_text())
    d["schema_version"] = 2
    f.write_text(json.dumps(d))
    prof, results = load_profile(f)
    assert prof is None
    assert [r.code for r in results] == ["profile_schema_unsupported"]


def test_load_profile_returns_profile_and_no_results_when_fine(tmp_path):
    from vml_supervisor.profile import load_profile
    p = make_profile()
    f = tmp_path / "p.json"
    p.to_file(f)
    prof, results = load_profile(f)
    assert prof == p and results == []


def test_load_profile_turns_a_malformed_file_into_a_result(tmp_path):
    from vml_supervisor.profile import load_profile
    f = tmp_path / "p.json"
    f.write_text("{not json")
    prof, results = load_profile(f)
    assert prof is None and results[0].code == "profile_malformed"
    prof, results = load_profile(tmp_path / "missing.json")
    assert prof is None and results[0].code == "profile_malformed"
    f.write_text("[1]")
    assert load_profile(f)[0] is None
    f.write_text('{"name": "x"}')
    assert load_profile(f)[1][0].code == "profile_malformed"


def test_validate_flags_an_unknown_hook_name():
    p = make_profile(hooks={"bogus": "m:f", "on_result": "m:g"})
    assert [(r.code, r.detail) for r in p.validate()] == [("hook_unknown", "bogus")]


def test_from_file_and_from_dict_refuse_a_newer_schema(tmp_path):
    from vml_supervisor.profile import HostProfile
    f = tmp_path / "p.json"
    make_profile().to_file(f)
    d = json.loads(f.read_text())
    d["schema_version"] = 2
    f.write_text(json.dumps(d))
    with pytest.raises(ValueError):
        HostProfile.from_file(f)
    with pytest.raises(ValueError):
        HostProfile.from_dict(d)


def _file_with(tmp_path, **over):
    f = tmp_path / "p.json"
    make_profile().to_file(f)
    d = json.loads(f.read_text())
    d.update(over)
    f.write_text(json.dumps(d))
    return f


def test_unknown_top_level_keys_are_results_and_the_profile_still_loads(tmp_path):
    from vml_supervisor.profile import load_profile
    f = _file_with(tmp_path, mode_source={"kind": "none"}, zzz=1)
    prof, results = load_profile(f)
    assert prof == make_profile()
    assert [(r.code, r.detail) for r in results] == [
        ("profile_key_unknown", "mode_source"), ("profile_key_unknown", "zzz")]


@pytest.mark.parametrize("v", [0, -1])
def test_schema_version_below_one_is_malformed(tmp_path, v):
    from vml_supervisor.profile import load_profile
    prof, results = load_profile(_file_with(tmp_path, schema_version=v))
    assert prof is None and results[0].code == "profile_malformed"


def test_hook_paths_given_as_a_string_is_malformed(tmp_path):
    from vml_supervisor.profile import load_profile
    prof, results = load_profile(_file_with(tmp_path, hook_paths="/r/hooks"))
    assert prof is None and results[0].code == "profile_malformed"


def test_a_helpers_status_argv_round_trips_and_is_optional(tmp_path):
    """FELIX-voice point 8: a helper may name the command that prints its own status line."""
    from vml_supervisor.profile import HelperSpec, HostProfile, load_profile
    spec = HelperSpec(["python", "barge.py"], "barge.py", "barge.log", (), True,
                      status_argv=["python", "barge.py", "status"])
    p = make_profile(helpers={"barge": spec})
    f = tmp_path / "profile.json"
    p.to_file(f)
    assert HostProfile.from_file(f).helpers["barge"].status_argv == ["python", "barge.py", "status"]
    data = json.loads(f.read_text())
    del data["helpers"]["barge"]["status_argv"]
    assert data["schema_version"] == 1
    f.write_text(json.dumps(data))
    loaded, problems = load_profile(f)
    assert problems == [] and loaded.helpers["barge"].status_argv == []
