"""patches/patch_stt_language.py: the STT request follows the pill's language.

Stephan saw a transcription "in another language": Whisper guesses the language of every
recording. When the pill's language override (voice_pill_state.get_override(pid)) is set
for this session, its ISO code goes to the STT endpoint as `language`; unset, today's
behaviour (auto-detect, or VOICEMODE_WHISPER_LANGUAGE). Run in a stand-in package with the
real voice_pill_state.py and voice_languages.py; applied to pristine 8.12.0 with VM812_SRC."""
import asyncio
import importlib
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
PATCHER = PATCHES / "patch_stt_language.py"

FAILOVER = '''\
import logging
from .config import WHISPER_LANGUAGE

logger = logging.getLogger("voicemode")


def is_local_provider(url):
    return "127.0.0.1" in url


async def simple_stt_failover(
    audio_file,
    base_url="http://127.0.0.1:2022/v1",
):
    for _ in [1]:
        try:
            transcription_kwargs = {"model": "whisper-1"}

            # Handle language parameter based on provider
            if WHISPER_LANGUAGE and WHISPER_LANGUAGE != "auto":
                # Explicit language set - pass to all providers
                transcription_kwargs["language"] = WHISPER_LANGUAGE
            elif is_local_provider(base_url):
                # Local whisper.cpp with auto mode - must pass "auto" explicitly
                transcription_kwargs["language"] = "auto"
            return transcription_kwargs
        except Exception:
            raise
'''


def _run(target):
    return subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True, text=True)


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(tmp_path / "base"))
    made = []

    def build(whisper_language="auto"):
        name = "vmsl_" + uuid.uuid4().hex[:8]
        pkg = tmp_path / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "config.py").write_text(f"WHISPER_LANGUAGE = {whisper_language!r}\n")
        (pkg / "simple_failover.py").write_text(FAILOVER)
        for module in ("voice_pill_state.py", "voice_languages.py"):
            shutil.copy(PATCHES / module, pkg / module)
        r = _run(pkg / "simple_failover.py")
        assert r.returncode == 0, r.stdout + r.stderr
        monkeypatch.syspath_prepend(str(tmp_path))
        made.append(name)
        failover = importlib.import_module(f"{name}.simple_failover")
        state = importlib.import_module(f"{name}.voice_pill_state")
        return failover, state

    yield build
    for name in made:
        for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
            del sys.modules[key]


def _kwargs(failover, base_url="http://127.0.0.1:2022/v1"):
    return asyncio.run(failover.simple_stt_failover(None, base_url))


@pytest.mark.parametrize("language,code", [("German", "de"), ("English", "en"), ("Spanish", "es"),
                                           ("Japanese", "ja"), ("Korean", "ko"), ("Dutch", "nl")])
def test_the_pills_language_goes_to_the_stt_endpoint(make, language, code):
    failover, state = make()
    state.set_override(os.getpid(), None, language)
    assert _kwargs(failover)["language"] == code
    assert _kwargs(failover, "https://api.openai.com/v1")["language"] == code   # OpenAI's param too


def test_it_wins_over_whisper_language(make):
    failover, state = make("en")
    state.set_override(os.getpid(), None, "German")
    assert _kwargs(failover)["language"] == "de"


def test_unset_is_todays_behaviour(make):
    failover, _ = make()
    assert _kwargs(failover)["language"] == "auto"                     # local whisper: auto
    assert "language" not in _kwargs(failover, "https://api.openai.com/v1")   # OpenAI: omitted
    failover_en, _ = make("en")
    assert _kwargs(failover_en)["language"] == "en"


def test_a_voice_only_override_or_an_unknown_name_changes_nothing(make):
    failover, state = make()
    state.set_override(os.getpid(), "af_bella", None)
    assert _kwargs(failover)["language"] == "auto"
    state.set_override(os.getpid(), None, "Klingon")
    assert _kwargs(failover)["language"] == "auto"


def test_another_sessions_override_is_not_ours(make):
    failover, state = make()
    state.set_override(os.getppid(), None, "German")
    assert _kwargs(failover)["language"] == "auto"


@pytest.fixture
def no_settings(tmp_path, monkeypatch):
    """The remember switch from a throwaway HOME and the environment only."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VOICEMODE_PILL_REMEMBER_LAST", raising=False)


# A first call takes its voice from the pill's last choice (no override yet): the recognition
# language must come from the same place (2026-11-18).
def test_the_last_choice_language_is_used_without_an_override(make, no_settings):
    failover, state = make()
    state.set_last_choice("dm_martin", "German")
    assert _kwargs(failover)["language"] == "de"


def test_the_override_wins_over_the_last_choice(make, no_settings):
    failover, state = make()
    state.set_last_choice("dm_martin", "German")
    state.set_override(os.getpid(), None, "Spanish")
    assert _kwargs(failover)["language"] == "es"


def test_remember_last_off_ignores_the_last_choice_for_stt(make, no_settings, monkeypatch):
    failover, state = make()
    state.set_last_choice("dm_martin", "German")
    monkeypatch.setenv("VOICEMODE_PILL_REMEMBER_LAST", "false")
    assert _kwargs(failover)["language"] == "auto"


def test_a_broken_state_never_breaks_stt(make, monkeypatch):
    failover, state = make()

    def boom(*args, **kwargs):
        raise OSError("disk gone")
    monkeypatch.setattr(state, "get_override", boom)
    assert _kwargs(failover)["language"] == "auto"


def test_idempotent_and_drift(tmp_path):
    target = tmp_path / "simple_failover.py"
    target.write_text(FAILOVER)
    assert _run(target).returncode == 0
    once = target.read_bytes()
    r = _run(target)
    assert r.returncode == 0 and "already patched (stt language)" in r.stdout and target.read_bytes() == once
    drift = tmp_path / "d" / "simple_failover.py"
    drift.parent.mkdir()
    drift.write_text(FAILOVER.replace('WHISPER_LANGUAGE != "auto"', "WHISPER_LANGUAGE not in ('auto',)"))
    before = drift.read_bytes()
    r = _run(drift)
    assert r.returncode == 1 and "ANCHOR DRIFT" in r.stderr and drift.read_bytes() == before


@pytest.mark.skipif(not os.environ.get("VM812_SRC"), reason="VM812_SRC is not set")
def test_applies_to_pristine_8_12_0(tmp_path):
    target = tmp_path / "simple_failover.py"
    shutil.copy(Path(os.environ["VM812_SRC"]) / "simple_failover.py", target)
    r = _run(target)
    assert r.returncode == 0, r.stderr
    text = target.read_text(encoding="utf-8")
    assert "_vml_stt_language()" in text and "STT LANGUAGE (voicemode-local)" in text
