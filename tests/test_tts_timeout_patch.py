"""Tests for the TTS-timeout edit in patches/patch_simple_failover.py.

Upstream builds every TTS client with a hardcoded 30 s timeout and no SDK retries for
local endpoints. A local Kokoro under load (Windows, 2026-11-13, n=1: 12.4 s of audio took
40.9 s) times out mid-sentence and the turn is lost. The edit makes the timeout
configurable with the same shape as the STT knob; defaults stay 30 s.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHER = REPO / "patches" / "patch_simple_failover.py"

FIXTURE = '''\
def is_local_provider(base_url):
    return "127.0.0.1" in base_url or "localhost" in base_url


def _prepare_tts_endpoint(base_url, voice, model, clone_profile):
    max_retries = 0 if is_local_provider(base_url) else 2
    client = dict(
        base_url=base_url,
        timeout=30.0,  # Reasonable timeout
        max_retries=max_retries
    )
    return client
'''

_spec = importlib.util.spec_from_file_location("patch_simple_failover", PATCHER)
patcher_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patcher_mod)


def run(target):
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = patcher_mod.apply_tts_timeout_knob(target)
    return rc, out.getvalue(), err.getvalue()


def load(target):
    spec = importlib.util.spec_from_file_location("patched_failover_tts", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("VOICEMODE_TTS_TIMEOUT", "VOICEMODE_TTS_TIMEOUT_LOCAL"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def target(tmp_path):
    p = tmp_path / "simple_failover.py"
    p.write_text(FIXTURE)
    return p


LOCAL, REMOTE = "http://127.0.0.1:18880/v1", "https://api.openai.com/v1"


def test_the_client_timeout_comes_from_the_knob(target):
    assert run(target)[0] == 0
    src = target.read_text()
    assert "timeout=30.0,  # Reasonable timeout" not in src
    assert load(target)._prepare_tts_endpoint(LOCAL, "af_sky", None, None)["timeout"] == 30.0


def test_defaults_are_unchanged(target):
    run(target)
    mod = load(target)
    assert mod._tts_timeout(LOCAL) == 30.0 and mod._tts_timeout(REMOTE) == 30.0


def test_local_override_applies_to_local_only(target, monkeypatch):
    run(target)
    mod = load(target)
    monkeypatch.setenv("VOICEMODE_TTS_TIMEOUT_LOCAL", "90")
    assert mod._tts_timeout(LOCAL) == 90.0 and mod._tts_timeout(REMOTE) == 30.0
    assert mod._prepare_tts_endpoint(LOCAL, "af_sky", None, None)["timeout"] == 90.0


def test_global_override_and_local_wins(target, monkeypatch):
    run(target)
    mod = load(target)
    monkeypatch.setenv("VOICEMODE_TTS_TIMEOUT", "45")
    assert mod._tts_timeout(LOCAL) == 45.0 and mod._tts_timeout(REMOTE) == 45.0
    monkeypatch.setenv("VOICEMODE_TTS_TIMEOUT_LOCAL", "120")
    assert mod._tts_timeout(LOCAL) == 120.0 and mod._tts_timeout(REMOTE) == 45.0


def test_a_bad_value_falls_back_loudly_not_silently(target, monkeypatch, caplog):
    run(target)
    mod = load(target)
    monkeypatch.setenv("VOICEMODE_TTS_TIMEOUT_LOCAL", "ninety")
    import logging
    with caplog.at_level(logging.WARNING):
        assert mod._tts_timeout(LOCAL) == 30.0
    assert "VOICEMODE_TTS_TIMEOUT_LOCAL" in caplog.text


def test_is_idempotent(target):
    run(target)
    once = target.read_text()
    rc, out, _ = run(target)
    assert rc == 0 and "already patched (tts timeout knob)" in out
    assert target.read_text() == once


def test_fails_loudly_on_drift(target):
    target.write_text(FIXTURE.replace("timeout=30.0,  # Reasonable timeout", "timeout=20.0,"))
    rc, _, err = run(target)
    assert rc == 1 and "ANCHOR DRIFT" in err


def test_the_cli_applies_it(target):
    r = subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True, text=True)
    assert "_tts_timeout(base_url)" in target.read_text(), r.stderr


@pytest.mark.skipif(not os.environ.get("VM812_SRC"), reason="VM812_SRC is not set")
def test_applies_to_pristine_8_12_0(tmp_path):
    t = tmp_path / "simple_failover.py"
    shutil.copy(Path(os.environ["VM812_SRC"]) / "simple_failover.py", t)
    assert run(t)[0] == 0
    assert "timeout=_tts_timeout(base_url)" in t.read_text()
