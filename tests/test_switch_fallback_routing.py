"""voicemode-switch routing with the managed CPU fallbacks on.

  STT_CPU_FALLBACK=true (STT_ENGINE=faster-whisper)
      VOICEMODE_STT_BASE_URLS gets http://127.0.0.1:<STT_CPU_FALLBACK_PORT>/v1 right after 2022
  KOKORO_CPU_FALLBACK=true (KOKORO_ONNX_GPU=true)
      VOICEMODE_TTS_BASE_URLS gets http://127.0.0.1:<KOKORO_CPU_FALLBACK_PORT>/v1 after the
      Kokoro and Piper URLs (local: 8880, 8881, 8882), before OpenAI; and
      VOICEMODE_TTS_TIMEOUT_LOCAL=90 is written unless the file already sets it
  both off
      the lists are exactly what they were

Runs the real script with HOME in a temp dir, so the user's ~/.voicemode and
~/.voicemode-local are never touched.
"""
import os
import subprocess
from pathlib import Path

import pytest

SWITCH = Path(__file__).resolve().parent.parent / "voicemode-switch"
K = "http://127.0.0.1:8880/v1"
P = "http://127.0.0.1:8881/v1"
KC = "http://127.0.0.1:8882/v1"
W = "http://127.0.0.1:2022/v1"
WC = "http://127.0.0.1:2023/v1"
O = "https://api.openai.com/v1"

BOTH = "STT_ENGINE=faster-whisper\nSTT_CPU_FALLBACK=true\nKOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\nKOKORO_CPU_FALLBACK=true\n"


def run(home: Path, config: str, *args, env_text=None):
    (home / ".voicemode-local").mkdir(exist_ok=True)
    (home / ".voicemode-local/config").write_text(config)
    if env_text is not None:
        (home / ".voicemode").mkdir(exist_ok=True)
        (home / ".voicemode/voicemode.env").write_text(env_text)
    done = subprocess.run([str(SWITCH), *args], env={**os.environ, "HOME": str(home)},
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    return (home / ".voicemode/voicemode.env").read_text()


def value(text, key):
    found = [l.split("=", 1)[1] for l in text.splitlines() if l.startswith(key + "=")]
    return found[-1] if found else None


@pytest.mark.parametrize("mode,stt,tts", [
    ("local", [W, WC, O], [K, P, KC, O]),
    ("localonly", [W, WC], [K, P, KC]),
    ("hybrid", [O, W, WC], [K, P, KC, O]),
    ("piper", [W, WC, O], [P, K, KC, O]),
])
def test_fallbacks_on_add_both_urls_after_their_engines(tmp_path, mode, stt, tts):
    text = run(tmp_path, BOTH, mode)
    assert value(text, "VOICEMODE_STT_BASE_URLS") == ",".join(stt)
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == ",".join(tts)
    assert value(text, "VOICEMODE_TTS_TIMEOUT_LOCAL") == "90"


@pytest.mark.parametrize("mode,stt,tts", [
    ("local", [W, O], [K, P, O]),
    ("localonly", [W], [K, P]),
    ("hybrid", [O, W], [K, P, O]),
    ("piper", [W, O], [P, K, O]),
])
def test_fallbacks_off_leave_the_lists_as_they_were(tmp_path, mode, stt, tts):
    text = run(tmp_path, "STT_ENGINE=faster-whisper\nKOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\n", mode)
    assert value(text, "VOICEMODE_STT_BASE_URLS") == ",".join(stt)
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == ",".join(tts)
    assert "VOICEMODE_TTS_TIMEOUT_LOCAL" not in text


def test_openai_mode_gets_no_local_fallback(tmp_path):
    text = run(tmp_path, BOTH, "openai")
    assert value(text, "VOICEMODE_STT_BASE_URLS") == O
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == O
    assert "VOICEMODE_TTS_TIMEOUT_LOCAL" not in text


def test_flags_without_their_gpu_engine_do_nothing(tmp_path):
    text = run(tmp_path, "STT_ENGINE=docker\nSTT_CPU_FALLBACK=true\nKOKORO_ONNX=true\nKOKORO_CPU_FALLBACK=true\n", "local")
    assert value(text, "VOICEMODE_STT_BASE_URLS") == f"{W},{O}"
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == f"{K},{P},{O}"
    assert "VOICEMODE_TTS_TIMEOUT_LOCAL" not in text


def test_only_the_stt_fallback(tmp_path):
    text = run(tmp_path, "STT_ENGINE=faster-whisper\nSTT_CPU_FALLBACK=true\n", "local")
    assert value(text, "VOICEMODE_STT_BASE_URLS") == f"{W},{WC},{O}"
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == f"{K},{P},{O}"
    assert "VOICEMODE_TTS_TIMEOUT_LOCAL" not in text


def test_only_the_kokoro_fallback(tmp_path):
    text = run(tmp_path, "KOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\nKOKORO_CPU_FALLBACK=true\n", "local")
    assert value(text, "VOICEMODE_STT_BASE_URLS") == f"{W},{O}"
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == f"{K},{P},{KC},{O}"
    assert value(text, "VOICEMODE_TTS_TIMEOUT_LOCAL") == "90"


def test_the_fallback_ports_come_from_the_config(tmp_path):
    cfg = BOTH + "STT_CPU_FALLBACK_PORT=12023\nKOKORO_CPU_FALLBACK_PORT=18882\n"
    text = run(tmp_path, cfg, "localonly")
    assert value(text, "VOICEMODE_STT_BASE_URLS") == f"{W},http://127.0.0.1:12023/v1"
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == f"{K},{P},http://127.0.0.1:18882/v1"


def test_a_timeout_the_user_set_is_never_overwritten(tmp_path):
    text = run(tmp_path, BOTH, "local", env_text="VOICEMODE_TTS_TIMEOUT_LOCAL=45\n")
    assert text.count("VOICEMODE_TTS_TIMEOUT_LOCAL=") == 1
    assert value(text, "VOICEMODE_TTS_TIMEOUT_LOCAL") == "45"


def test_an_exported_user_timeout_counts_as_set(tmp_path):
    text = run(tmp_path, BOTH, "local", env_text="export VOICEMODE_TTS_TIMEOUT_LOCAL=60\n")
    assert text.count("VOICEMODE_TTS_TIMEOUT_LOCAL=") == 1


def test_turning_the_kokoro_fallback_off_removes_the_timeout_it_wrote(tmp_path):
    run(tmp_path, BOTH, "local")
    text = run(tmp_path, "STT_ENGINE=faster-whisper\nKOKORO_ONNX=true\nKOKORO_ONNX_GPU=true\n", "local")
    assert "VOICEMODE_TTS_TIMEOUT_LOCAL" not in text
    assert value(text, "VOICEMODE_TTS_BASE_URLS") == f"{K},{P},{O}"


def test_the_same_mode_twice_is_byte_identical(tmp_path):
    once = run(tmp_path, BOTH, "local")
    assert run(tmp_path, BOTH, "local") == once


def test_the_fallback_urls_appear_once(tmp_path):
    text = run(tmp_path, BOTH, "local")
    assert value(text, "VOICEMODE_TTS_BASE_URLS").count(KC) == 1
    assert value(text, "VOICEMODE_STT_BASE_URLS").count(WC) == 1
