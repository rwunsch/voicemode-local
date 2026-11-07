"""_start_proxy in the piper-proxy tests never leaves a proxy running when it fails.

A health check that times out raises TimeoutError, which is no URLError: it used to escape
the loop and leave the proxy running, reparented to init once pytest ended (seen on
18881 and 18882, 2026-11-06; reproduced by the task 8 reviewer under load).
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeProc:
    def __init__(self, *args, **kwargs):
        self.stopped = self.killed = False
        FakeProc.last = self

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize("module", ["test_piper_proxy", "test_piper_proxy_errors"])
@pytest.mark.parametrize("error", [TimeoutError("timed out"), ConnectionResetError("reset"), ValueError("odd")])
def test_a_failed_start_always_stops_the_proxy(module, error, monkeypatch):
    mod = _load(module)
    monkeypatch.setattr(mod.subprocess, "Popen", FakeProc)

    def urlopen(*args, **kwargs):
        raise error

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(mod, "START_TIMEOUT", 0.3)
    with pytest.raises(Exception):
        mod._start_proxy()
    assert FakeProc.last.stopped or FakeProc.last.killed
