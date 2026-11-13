"""A FELIX profile over vml_supervisor, as a test fixture.

FELIX owns the real one (felix_voice_profile, in its voice.py). This proves an outside harness can
describe itself with FELIX's values (the research map's FELIX column), put its hooks on hook_paths
and drive the service lifecycle. The hook functions live in a module written under tmp_path, so
hook_paths and load-once are exercised the way a vendored harness uses them.
"""
import dataclasses
import json
import sys
import textwrap
from pathlib import Path

import pytest

from vml_supervisor import hooks as hook_calls
from vml_supervisor import helpers
from vml_supervisor.hooks import load_hooks
from vml_supervisor.profile import HostProfile, PortOverride
from vml_supervisor.services import check_service, ensure_services, release

# the fake world: loopback fakes standing in for Kokoro and whisper.cpp
from test_supervisor_services import alive, codes, read_state, wait_dead, world  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="acts out POSIX with fake programs")

FELIX_TTS = (18880, 18899)
FELIX_STT = (12022, 12041)

HOOK_MODULE = textwrap.dedent('''
    """felix_voice_profile_test: the example hooks of an outside harness."""
    import json
    from pathlib import Path

    def read_mode(profile, kind):
        """voice.barge from the preferences JSON; absent means the helper's default."""
        prefs = Path(profile["root"]) / "preferences.json"
        try:
            data = json.loads(prefs.read_text())
        except FileNotFoundError:
            return None
        value = data.get("voice", {}).get("barge")
        return None if value is None else str(value).lower()

    def pre_start(profile, runtime, env):
        """Stand-in for FELIX's microphone check: returns refusal text, or None to allow."""
        return "no microphone" if (Path(profile["root"]) / "no-mic").exists() else None

    def on_result(profile, results):
        """Stand-in for last-start.json."""
        Path(profile["run_dir"]).mkdir(parents=True, exist_ok=True)
        (Path(profile["run_dir"]) / "last-start.json").write_text(json.dumps(results))
''')


def felix_profile_example(root: Path, runtime: Path, hook_dir: Path, fake: Path,
                          ranges=(FELIX_TTS, FELIX_STT)) -> HostProfile:
    """FELIX's values: tts 18880-18899, stt 12022-12041, an override in <root>/ports.json, the
    markers FELIX's processes carry, and the three example hooks."""
    from vml_supervisor.profile import ServiceSpec
    root = Path(root)
    return HostProfile(
        name="felix-voice", root=root, run_dir=root / "run", runtime_dir=Path(runtime),
        python=Path(sys.executable), working_dir=root / "work", env={},
        services={
            "tts": ServiceSpec([sys.executable, str(fake), "tts", "--port", "{port}"], f"{fake} tts",
                               ranges[0], "/v1/models", "kokoro"),
            "stt": ServiceSpec([sys.executable, str(fake), "stt", "--port", "{port}"], f"{fake} stt",
                               ranges[1], "/health", "whisper"),
        },
        helpers={}, reserved_ports=(),
        port_override=PortOverride("file", root / "ports.json"),
        start_timeout=30.0, stop_timeout=5.0, watch_interval=3.0, restart_limit=(3, 60.0),
        lease_markers=("voice.py launch",), voice_server_marker="voice-mode",
        host_command=[], host_marker="voice.py host",
        watch_command=[sys.executable, "-c", "import time; time.sleep(600)", "voice.py", "watch"],
        watch_marker="voice.py watch", job_name="felix-voice-test",
        hooks={"read_mode": "felix_voice_profile_test:read_mode",
               "pre_start": "felix_voice_profile_test:pre_start",
               "on_result": "felix_voice_profile_test:on_result"},
        hook_paths=(Path(hook_dir),))


def test_felix_values_validate_and_run_the_service_lifecycle(world, tmp_path):  # noqa: F811
    hook_dir = tmp_path / "felix_hooks"
    hook_dir.mkdir()
    (hook_dir / "felix_voice_profile_test.py").write_text(HOOK_MODULE)
    base = world.profile
    felix = felix_profile_example(base.root, base.runtime_dir, hook_dir, world.fake)
    assert felix.validate() == []        # FELIX's ranges clear the reserved and the overlap checks
    assert felix.services["tts"].port_range == FELIX_TTS and felix.services["stt"].port_range == FELIX_STT

    # The override in <root>/ports.json is tried first; free ports stand in for FELIX's own.
    tts_port, stt_port = world.ports["tts"][0], world.ports["stt"][0]
    (felix.root).mkdir(parents=True, exist_ok=True)
    (felix.root / "ports.json").write_text(json.dumps({"tts": tts_port, "stt": stt_port}))
    profile = dataclasses.replace(felix, run_dir=base.run_dir)

    before = list(sys.path)
    hooks, problems = load_hooks(profile)
    assert problems == [] and set(hooks.funcs) == {"read_mode", "pre_start", "on_result"}
    assert sys.path == before            # hook_paths is not left on sys.path
    # load-once: the host keeps these callables, whatever happens to the folder afterwards
    (hook_dir / "felix_voice_profile_test.py").write_text("raise RuntimeError('replaced')")
    chosen, results = ensure_services(profile, world.connector.pid, hooks)
    assert results == [] and chosen == {"tts": tts_port, "stt": stt_port}
    assert all(check_service(kind, port) for kind, port in chosen.items())
    state = read_state(profile)
    assert profile.lease_markers == ("voice.py launch",) and set(state) == {"tts", "stt"}

    # read_mode: voice.barge from the preferences JSON
    assert hook_calls.read_mode(hooks, profile, "barge") == (None, None)
    (profile.root / "preferences.json").write_text(json.dumps({"voice": {"barge": "AUTO"}}))
    assert hook_calls.read_mode(hooks, profile, "barge") == ("auto", None)
    assert helpers.read_mode(profile, "barge", hooks, []) == "auto"

    # pre_start refuses with text; on_result writes last-start.json
    assert hook_calls.run_pre_start(hooks, profile, str(profile.runtime_dir), {}) is None
    (profile.root / "no-mic").write_text("")
    refused = hook_calls.run_pre_start(hooks, profile, str(profile.runtime_dir), {})
    assert refused is not None and refused.code == "pre_start_refused" and refused.detail == "no microphone"
    _, failed = hook_calls.on_result(hooks, profile, [{"code": "pre_start_refused"}])
    assert failed is None
    assert json.loads((profile.run_dir / "last-start.json").read_text()) == [{"code": "pre_start_refused"}]

    assert release(profile, world.connector.pid) == []
    assert all(wait_dead(entry["pid"]) for entry in state.values())
    sys.modules.pop("felix_voice_profile_test", None)


def test_every_result_code_is_in_the_readme():
    """vml_supervisor/README.md lists every Result code the package can produce."""
    import re
    package = Path(__file__).resolve().parents[1] / "vml_supervisor"
    found = set()
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        found |= set(re.findall(r'Result\("([A-Za-z0-9_]+)"', text))
        # codes chosen at run time: code = "a" if ... else "b"
        for line in re.findall(r'\bcode = (.+)', text):
            found |= set(re.findall(r'"([a-z_]+)"', line))
    readme = (package / "README.md").read_text(encoding="utf-8")
    missing = sorted(code for code in found if f"`{code}`" not in readme)
    assert found and missing == []
