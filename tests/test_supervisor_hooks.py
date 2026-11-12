"""Tests for vml_supervisor.hooks: named hooks, loaded once, never raising."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from tests.test_supervisor_profile import make_profile  # noqa: E402


def _write(dirpath, name, body):
    (dirpath / f"{name}.py").write_text(body)


def test_a_missing_hook_returns_the_default():
    from vml_supervisor import hooks as H
    p = make_profile(hooks={}, hook_paths=())
    h, res = H.load_hooks(p)
    assert res == []
    d = p.to_dict()
    assert H.call_hook(h, "anything", default="dflt") == ("dflt", None)
    assert H.resolve_runtime(h, p) == ([str(p.runtime_dir), True], None)
    assert H.environment(h, p, "/rt", {}, {}) == ({"A": "1"}, None)
    spec, r = H.service_command(h, p, "tts", "/rt", 8891)
    assert r is None and spec == [["python", "tts.py", "--port", "8891"], "tts.py"]
    assert H.service_command(h, p, "stt", "/rt", 9011)[0] == [["python", "stt.py"], "stt.py"]
    assert H.prepare_home(h, p, {}) == (None, None)
    assert H.read_mode(h, p, "tts") == (None, None)
    assert H.on_result(h, p, [{"code": "x"}]) == (None, None)
    assert H.run_pre_start(h, p, "/rt", {}) is None
    assert d["name"] == "test"


def test_a_hook_is_loaded_from_its_entry_point(tmp_path):
    from vml_supervisor import hooks as H
    _write(tmp_path, "hk_a", "def mode(profile, kind):\n    return 'm-' + kind\n")
    p = make_profile(hooks={"read_mode": "hk_a:mode"}, hook_paths=(tmp_path,))
    h, res = H.load_hooks(p)
    assert res == []
    assert H.read_mode(h, p, "tts") == ("m-tts", None)
    assert str(tmp_path) not in sys.path


def test_hooks_are_loaded_once_and_survive_their_module_being_replaced(tmp_path):
    from vml_supervisor import hooks as H
    f = tmp_path / "hk_b.py"
    f.write_text("def mode(profile, kind):\n    return 'one'\n")
    p = make_profile(hooks={"read_mode": "hk_b:mode"}, hook_paths=(tmp_path,))
    h, _ = H.load_hooks(p)
    f.write_text("def mode(profile, kind):\n    return 'two'\n")
    assert H.read_mode(h, p, "tts")[0] == "one"
    f.unlink()
    assert H.read_mode(h, p, "tts")[0] == "one"


def test_a_failing_hook_is_a_result_not_an_exception(tmp_path):
    from vml_supervisor import hooks as H
    _write(tmp_path, "hk_c", "def boom(*a):\n    raise ValueError('x')\n")
    p = make_profile(hooks={"read_mode": "hk_c:boom", "environment": "hk_c:boom"},
                     hook_paths=(tmp_path,))
    h, _ = H.load_hooks(p)
    val, r = H.read_mode(h, p, "tts")
    assert val is None and r.code == "hook_failed" and "read_mode" in r.detail and "ValueError" in r.detail
    env, r = H.environment(h, p, "/rt", {}, {})
    assert env == {"A": "1"} and r.code == "hook_failed"


def test_a_hook_that_fails_to_load_is_a_result_and_others_still_load(tmp_path):
    from vml_supervisor import hooks as H
    _write(tmp_path, "hk_d", "def ok(profile, kind):\n    return 'fine'\n")
    p = make_profile(hooks={"read_mode": "hk_d:ok", "on_result": "no_such_mod_xyz:f",
                            "pre_start": "hk_d:missing", "environment": "badentry"},
                     hook_paths=(tmp_path,))
    h, res = H.load_hooks(p)
    assert sorted(r.code for r in res) == ["hook_failed"] * 3
    assert H.read_mode(h, p, "tts")[0] == "fine"
    assert H.on_result(h, p, []) == (None, None)


def test_unknown_hook_names_are_not_loaded(tmp_path):
    from vml_supervisor import hooks as H
    _write(tmp_path, "hk_e", "def f(*a):\n    return 1\n")
    p = make_profile(hooks={"bogus": "hk_e:f"}, hook_paths=(tmp_path,))
    h, res = H.load_hooks(p)
    assert res == [] and H.call_hook(h, "bogus", default="d") == ("d", None)


def test_hooks_receive_plain_data_and_return_values(tmp_path):
    from vml_supervisor import hooks as H
    _write(tmp_path, "hk_f", (
        "def rt(pd):\n    return [pd['name'] + '-rt', False]\n"
        "def env(pd, runtime, ports, modes):\n    return {'R': runtime, 'P': str(ports['tts'])}\n"
        "def svc(pd, kind, runtime, port):\n    return [[runtime, kind, str(port)], 'mk']\n"
        "def home(pd, env):\n    env['HOMED'] = '1'\n"
        "def pre(pd, runtime, env):\n    return 'no mic'\n"
        "def res(pd, results):\n    pd['seen'] = len(results)\n"))
    p = make_profile(hooks={"resolve_runtime": "hk_f:rt", "environment": "hk_f:env",
                            "service_command": "hk_f:svc", "prepare_home": "hk_f:home",
                            "pre_start": "hk_f:pre", "on_result": "hk_f:res"},
                     hook_paths=(tmp_path,))
    h, res = H.load_hooks(p)
    assert res == []
    assert H.resolve_runtime(h, p) == (["test-rt", False], None)
    assert H.environment(h, p, "/rt", {"tts": 1}, {}) == ({"R": "/rt", "P": "1"}, None)
    assert H.service_command(h, p, "tts", "/rt", 5) == ([["/rt", "tts", "5"], "mk"], None)
    env = {}
    assert H.prepare_home(h, p, env) == (None, None) and env == {"HOMED": "1"}
    r = H.run_pre_start(h, p, "/rt", {})
    assert r.code == "pre_start_refused" and r.detail == "no mic"
    assert H.on_result(h, p, [{"code": "a"}]) == (None, None)


def test_pre_start_empty_or_none_allows_and_failure_is_reported(tmp_path):
    from vml_supervisor import hooks as H
    _write(tmp_path, "hk_g", "def none(*a):\n    return None\ndef empty(*a):\n    return ''\n"
                             "def boom(*a):\n    raise RuntimeError('b')\n")
    for entry, code in (("hk_g:none", None), ("hk_g:empty", None), ("hk_g:boom", "hook_failed")):
        p = make_profile(hooks={"pre_start": entry}, hook_paths=(tmp_path,))
        h, _ = H.load_hooks(p)
        r = H.run_pre_start(h, p, "/rt", {})
        assert (r.code if r else None) == code
