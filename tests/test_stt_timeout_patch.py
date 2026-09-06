"""Tests for the STT-timeout edit in patches/patch_simple_failover.py.

The fixture is a synthetic minimum: just the two anchors the patcher keys on,
plus enough scaffolding to import. That is deliberate — this file tests the
*patcher's* behaviour (substitution, idempotence, drift detection) and the
resulting _stt_timeout semantics. Whether the anchors still match real upstream
is a different question, answered by the audit tooling against pristine 8.12.0
sources, not by a vendored blob that goes stale silently.

Run: pytest tests/test_stt_timeout_patch.py
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHER = REPO / "patches" / "patch_simple_failover.py"

# The two anchors, in their upstream 8.12.0 spelling, plus a stub
# is_local_provider so the patched module is importable on its own.
FIXTURE = '''\
def is_local_provider(base_url):
    return "127.0.0.1" in base_url or "localhost" in base_url


def _is_transient_stt_error(e: Exception) -> bool:
    return True


async def simple_stt_failover(audio_file):
    for i, base_url in enumerate(["http://127.0.0.1:2022/v1"]):
        try:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=60.0,  # Allow time for slower transcriptions
                max_retries=max_retries
            )
        except Exception:
            raise
'''


# The patcher carries two independent edits and the CLI runs both. These tests
# drive the timeout edit directly, so a fixture only has to carry that edit's
# anchors; test_edits_are_independent covers the CLI wiring.
_spec = importlib.util.spec_from_file_location("patch_simple_failover", PATCHER)
patcher_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patcher_mod)


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run(target: Path, capsys=None):
    """Apply the timeout edit, capturing what the patcher printed."""
    import io
    import contextlib
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = patcher_mod.apply_timeout_knob(target)
    return _Result(rc, out.getvalue(), err.getvalue())


def load(target: Path):
    spec = importlib.util.spec_from_file_location("patched_failover", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Hermetic env.

    voice_mode.config.load_voicemode_env() copies ~/.voicemode/voicemode.env
    into os.environ at import time, so on a machine that has opted in, these
    vars are already set when the suite runs together (but not when this file
    runs alone). Clear them so the tests assert the patch's semantics rather
    than the developer's config.
    """
    monkeypatch.delenv("VOICEMODE_STT_TIMEOUT", raising=False)
    monkeypatch.delenv("VOICEMODE_STT_TIMEOUT_LOCAL", raising=False)


@pytest.fixture
def target(tmp_path) -> Path:
    p = tmp_path / "simple_failover.py"
    p.write_text(FIXTURE)
    return p


def test_replaces_the_hardcoded_timeout(target):
    assert run(target).returncode == 0
    src = target.read_text()
    assert "timeout=60.0,  # Allow time for slower transcriptions" not in src
    assert "timeout=_stt_timeout(base_url)" in src


def test_helper_lands_above_its_anchor(target):
    run(target)
    src = target.read_text()
    assert src.index("def _stt_timeout(") < src.index("def _is_transient_stt_error(")


def test_defaults_are_unchanged_so_the_patch_is_a_noop(target):
    """The whole point: nobody's behaviour changes until they opt in."""
    run(target)
    mod = load(target)
    assert mod._stt_timeout("http://127.0.0.1:2022/v1") == 60.0
    assert mod._stt_timeout("https://api.openai.com/v1") == 60.0


def test_local_override_applies_to_local_only(target, monkeypatch):
    run(target)
    mod = load(target)
    monkeypatch.setenv("VOICEMODE_STT_TIMEOUT_LOCAL", "15")
    assert mod._stt_timeout("http://127.0.0.1:2022/v1") == 15.0
    assert mod._stt_timeout("https://api.openai.com/v1") == 60.0


def test_global_override_applies_to_both(target, monkeypatch):
    run(target)
    mod = load(target)
    monkeypatch.setenv("VOICEMODE_STT_TIMEOUT", "20")
    assert mod._stt_timeout("http://127.0.0.1:2022/v1") == 20.0
    assert mod._stt_timeout("https://api.openai.com/v1") == 20.0


def test_local_override_wins_over_global(target, monkeypatch):
    run(target)
    mod = load(target)
    monkeypatch.setenv("VOICEMODE_STT_TIMEOUT", "20")
    monkeypatch.setenv("VOICEMODE_STT_TIMEOUT_LOCAL", "5")
    assert mod._stt_timeout("http://127.0.0.1:2022/v1") == 5.0
    assert mod._stt_timeout("https://api.openai.com/v1") == 20.0


def test_is_idempotent(target):
    assert run(target).returncode == 0
    once = target.read_text()
    r = run(target)
    assert r.returncode == 0
    assert "already patched (stt timeout knob)" in r.stdout
    assert target.read_text() == once


def test_fails_loudly_when_the_timeout_anchor_drifts(target):
    target.write_text(FIXTURE.replace("timeout=60.0,  # Allow time for slower transcriptions",
                                      "timeout=45.0,  # upstream changed its mind"))
    r = run(target)
    assert r.returncode == 1
    assert "ANCHOR DRIFT" in r.stderr


def test_fails_loudly_when_the_helper_anchor_drifts(target):
    target.write_text(FIXTURE.replace("def _is_transient_stt_error(e: Exception) -> bool:",
                                      "def _is_transient(e):"))
    r = run(target)
    assert r.returncode == 1
    assert "ANCHOR DRIFT" in r.stderr


def test_edits_are_independent(target):
    """Drift in the voice-swap edit must not silently skip the timeout edit.

    The fixture deliberately carries no voice-mapping block, so edit 1 always
    reports drift here — the CLI must still apply edit 2 and still exit 1.
    """
    r = subprocess.run([sys.executable, str(PATCHER), str(target)],
                       capture_output=True, text=True)
    assert r.returncode == 1, "a drifted edit must still fail the run"
    assert "ANCHOR DRIFT" in r.stderr
    assert "timeout=_stt_timeout(base_url)" in target.read_text()
