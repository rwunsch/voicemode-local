"""Guard: no test can resolve Claude Code's real session registry (2026-11-19).

Stray "Voice queue: your turn..." messages reached real Claude Code sessions during
test runs. session_wake finds sessions through <claude dir>/sessions/<pid>.json
(CLAUDE_CONFIG_DIR, else ~/.claude), and every messaging socket path it would use
comes from those files. conftest's per-test fixture covered code running inside a
test, but not code running at import time, in module- or session-scoped fixtures,
or after a test's teardown (a daemon notify thread, for example): there
CLAUDE_CONFIG_DIR was unset and the lookup fell back to the real ~/.claude.

conftest now points CLAUDE_CONFIG_DIR at a session-wide temp dir when it is
imported, before any test module. These checks fail if any of those places
resolves a path outside that temp tree.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("_isolation_session_wake", REPO / "patches" / "session_wake.py")
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

ROOT_VAR = "VML_TEST_ISOLATION_ROOT"


def _root():
    raw = os.environ.get(ROOT_VAR)
    assert raw, f"conftest did not set {ROOT_VAR}: the session-wide isolation is missing"
    return Path(raw).resolve()


def _inside(path, root):
    path = Path(path).resolve()
    return path == root or root in path.parents


def _claude_paths():
    return {"claude_dir": sw.claude_dir(), "sessions": sw._sessions_dir()}


AT_IMPORT = {"root": os.environ.get(ROOT_VAR), "paths": _claude_paths()}
LATER = {}


def test_the_registry_is_isolated_already_when_test_modules_are_imported():
    assert AT_IMPORT["root"], f"{ROOT_VAR} was not set at import time"
    root = Path(AT_IMPORT["root"]).resolve()
    for name, path in AT_IMPORT["paths"].items():
        assert _inside(path, root), f"{name} at import time is {path}, outside {root}"


@pytest.fixture(scope="module")
def module_scoped():
    """Module fixtures are set up before the per-test fixture, and torn down after it."""
    LATER["setup"] = _claude_paths()
    yield
    LATER["teardown"] = _claude_paths()


def test_the_registry_is_isolated_in_a_module_scoped_fixture(module_scoped):
    root = _root()
    for name, path in LATER["setup"].items():
        assert _inside(path, root), f"{name} in a module fixture is {path}, outside {root}"


def test_the_registry_is_isolated_inside_a_test():
    root = _root()
    for name, path in _claude_paths().items():
        assert _inside(path, root), f"{name} inside a test is {path}, outside {root}"


def test_after_a_teardown_the_registry_stays_isolated(monkeypatch):
    # What a daemon thread sees once a test's monkeypatch has been undone.
    root = _root()
    monkeypatch.undo()
    for name, path in _claude_paths().items():
        assert _inside(path, root), f"{name} after teardown is {path}, outside {root}"


def test_a_child_process_inherits_the_isolation():
    root = _root()
    code = ("import importlib.util, sys; s = importlib.util.spec_from_file_location('w', sys.argv[1]); "
            "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.claude_dir())")
    out = subprocess.run([sys.executable, "-c", code, str(REPO / "patches" / "session_wake.py")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert _inside(out.stdout.strip(), root), out.stdout


def test_this_process_has_no_claude_session_to_find():
    # pytest run from a Claude Code session has that session among its ancestors: with
    # the real registry this would name it. Isolated, there is no registry to find.
    assert sw.claude_pid_for(os.getpid()) is None
    assert not list(sw._sessions_dir().glob("*.json"))
