"""patches/patch_voice_fallback.py: the wrappers it appends to simple_failover.py and
tools/converse.py, run in a small stand-in package (any package name: the wrappers import
relatively), and its application to pristine voice-mode 8.12.0 (VM812_SRC)."""
import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
PATCHER = PATCHES / "patch_voice_fallback.py"

FAILOVER = '''\
from typing import Optional, Tuple, Dict, Any
from .config import TTS_BASE_URLS

CALLS = []
INSTALLED = {"af_sky", "bm_george"}


def is_local_provider(url):
    return "127.0.0.1" in url


def _failure(voice):
    return {"error_type": "all_providers_failed", "attempted_endpoints": [
        {"endpoint": TTS_BASE_URLS[0] + "/audio/speech", "voice": voice,
         "error": "Error code: 404 - {'error': \\"Voice '%s' not found\\"}" % voice}]}


async def simple_tts_failover(
    text: str,
    voice: str,
    model: Optional[str] = None,
    **kwargs
):
    CALLS.append(("play", voice, kwargs))
    if voice in INSTALLED:
        return True, {}, {"voice": voice}
    return False, None, _failure(voice)


async def simple_tts_synthesize(
    text: str,
    voice: str,
    model: Optional[str] = None,
    **kwargs
):
    CALLS.append(("synth", voice, kwargs))
    if voice in INSTALLED:
        return True, [0.0], 24000, {}, {"voice": voice}
    return False, None, None, None, _failure(voice)
'''

CONVERSE = '''\
async def _converse_core(
    message=None,
    voice=None,
):
    from ..simple_failover import simple_tts_failover
    ok, _, config = await simple_tts_failover(message, voice)
    return "spoken" if ok else "failed"


async def converse(message=None, voice=None):
    result = await _converse_core(message=message, voice=voice)
    return result + " | widgets"
'''


class _Voices(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = json.dumps({"voices": ["af_sky", "bm_george", "ef_dora"]}).encode()
        self.send_response(200 if self.path == "/v1/audio/voices" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def voices_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Voices)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()


def _run(target):
    return subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True, text=True)


@pytest.fixture
def package(tmp_path, voices_server, monkeypatch):
    name = "vmfb_" + uuid.uuid4().hex[:8]
    pkg = tmp_path / name
    (pkg / "tools").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "tools" / "__init__.py").write_text("")
    (pkg / "config.py").write_text(f"TTS_BASE_URLS = [{voices_server!r}, 'https://api.openai.com/v1']\n"
                                   "TTS_VOICES = ['bm_george', 'af_sky']\n")
    (pkg / "simple_failover.py").write_text(FAILOVER)
    (pkg / "tools" / "converse.py").write_text(CONVERSE)
    for module in ("voice_languages.py", "voice_fallback.py"):
        shutil.copy(PATCHES / module, pkg / module)
    for rel in ("simple_failover.py", "tools/converse.py"):
        r = _run(pkg / rel)
        assert r.returncode == 0, r.stdout + r.stderr
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name, pkg
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]


def test_an_unknown_voice_is_spoken_with_an_installed_one_and_converse_says_so(package):
    name, _ = package
    converse = importlib.import_module(f"{name}.tools.converse")
    failover = importlib.import_module(f"{name}.simple_failover")
    result = asyncio.run(converse.converse(message="Hello", voice="bf_nobody"))
    assert result == "spoken | Voice bf_nobody is not installed here; spoke with bm_george. | widgets"
    assert [c[1] for c in failover.CALLS] == ["bf_nobody", "bm_george"]


def test_an_installed_voice_has_no_note(package):
    name, _ = package
    converse = importlib.import_module(f"{name}.tools.converse")
    assert asyncio.run(converse.converse(message="Hello", voice="af_sky")) == "spoken | widgets"


def test_the_synthesis_path_falls_back_too(package):
    name, _ = package
    failover = importlib.import_module(f"{name}.simple_failover")
    ok, samples, rate, _, config = asyncio.run(failover.simple_tts_synthesize("Hi", "af_nobody", None, speed=1.2))
    assert ok and config["voice"] == "bm_george" and config["requested_voice"] == "af_nobody"
    assert failover.CALLS == [("synth", "af_nobody", {"speed": 1.2}), ("synth", "bm_george", {"speed": 1.2})]


def test_patching_is_idempotent(package):
    _, pkg = package
    for rel in ("simple_failover.py", "tools/converse.py"):
        once = (pkg / rel).read_bytes()
        r = _run(pkg / rel)
        assert r.returncode == 0 and "already patched (voice fallback)" in r.stdout
        assert (pkg / rel).read_bytes() == once


def test_drift_fails_loudly_and_leaves_the_file(tmp_path):
    target = tmp_path / "simple_failover.py"
    target.write_text(FAILOVER.replace("async def simple_tts_synthesize(\n", "async def synthesize(\n"))
    before = target.read_bytes()
    r = _run(target)
    assert r.returncode == 1 and "ANCHOR DRIFT" in r.stderr
    assert target.read_bytes() == before


def test_crlf_is_kept(tmp_path):
    target = tmp_path / "converse.py"
    target.write_bytes(CONVERSE.replace("\n", "\r\n").encode())
    assert _run(target).returncode == 0
    data = target.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")


@pytest.mark.skipif(not os.environ.get("VM812_SRC"), reason="VM812_SRC is not set")
def test_applies_to_pristine_8_12_0_through_apply_py(tmp_path):
    src = Path(os.environ["VM812_SRC"])
    pkg = tmp_path / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    for name in ("control_channel.py", "control_socket.py", "core.py", "simple_failover.py", "streaming.py", "audio_player.py",
                 "conch_notify.py", "conch_queue.py", "conch.py", "config.py"):
        shutil.copy(src / name, pkg / name)
    shutil.copy(src / "converse.py", pkg / "tools" / "converse.py")
    shutil.copy(src / "configuration_management.py", pkg / "tools" / "configuration_management.py")
    r = subprocess.run([sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(pkg)],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    failover = (pkg / "simple_failover.py").read_text(encoding="utf-8")
    converse = (pkg / "tools" / "converse.py").read_text(encoding="utf-8")
    assert "VOICE FALLBACK (voicemode-local)" in failover and "VOICE FALLBACK (voicemode-local)" in converse
    # The fallback wraps whatever simple_tts_failover is at the end: tts_chunks' wrapper.
    assert failover.index("from .tts_chunks import speak_in_chunks") < failover.index(
        "_vml_simple_tts_failover_one_voice = simple_tts_failover")
    assert (pkg / "voice_fallback.py").is_file() and (pkg / "voice_languages.py").is_file()
