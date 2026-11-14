"""Long replies against a slow local TTS server, through the real patched voice-mode.

2026-11-14, Windows laptop: a 1,178-character reply was sent to the local Kokoro server
as one streaming request, timed out at 90 s, was sent again buffered and timed out
again; nothing was spoken. These tests rebuild that with a fake server that takes time
in proportion to the text, and voice-mode 8.12.0 patched by manifest.windows.json (the
list FELIX's Windows setup runs). Only the sound device is replaced; the HTTP client,
timeout, failover and fallback code are voice-mode's own.

The tree is the installed voice_mode package with the pristine 8.12.0 sources from
VM812_SRC laid over it, so the patch input is pristine whatever this venv was patched with.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_tts_long_text_live.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
SOURCES = {
    "control_channel.py": "control_channel.py", "control_socket.py": "control_socket.py",
    "core.py": "core.py", "simple_failover.py": "simple_failover.py", "streaming.py": "streaming.py",
    "server.py": "server.py", "tools/converse.py": "converse.py", "conch_notify.py": "conch_notify.py",
    "conch_queue.py": "conch_queue.py", "conch.py": "conch.py", "config.py": "config.py",
    "tools/configuration_management.py": "configuration_management.py",
}
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tts_chunks import LONG  # noqa: E402  the 1,178-character reply

TIMEOUT_S = 1.0          # VOICEMODE_TTS_TIMEOUT_LOCAL in the child
SECONDS_PER_CHAR = 0.003  # the fake server: LONG takes 3.5 s, a 200-character part 0.6 s
CHUNK_CHARS = 200

DRIVER = textwrap.dedent('''
    import asyncio, json, sys, threading, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    per_char = float(sys.argv[1])
    requests = []

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.endswith("/models"):
                self._json(200, {"object": "list", "data": [{"id": "tts-1", "object": "model"}]})
            else:
                self._json(200, {"status": "ok"})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            seconds = per_char * len(data["input"])
            requests.append({"chars": len(data["input"]), "text": data["input"], "seconds": seconds,
                             "format": data.get("response_format")})
            time.sleep(seconds)
            body = bytes(4800)  # 0.1 s of silence, 16-bit PCM at 24 kHz
            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/pcm")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass  # the client left (timed out)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    import os
    os.environ["VOICEMODE_TTS_BASE_URLS"] = f"http://127.0.0.1:{httpd.server_address[1]}/v1"

    import sounddevice

    class FakeOutputStream:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def write(self, data):
            pass

        def stop(self):
            pass

        def abort(self):
            pass

        def close(self):
            pass

    sounddevice.OutputStream = FakeOutputStream

    from voice_mode.simple_failover import simple_tts_failover

    text = sys.stdin.read()
    ok, metrics, config = asyncio.run(simple_tts_failover(text=text, voice="af_sky", audio_format="pcm"))
    errors = [a.get("error") for a in (config or {}).get("attempted_endpoints", [])]
    print("RESULT " + json.dumps({"ok": ok, "requests": requests, "errors": errors}))
''')


def _src() -> Path:
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    return Path(raw)


def _installed_voice_mode() -> Path:
    spec = importlib.util.find_spec("voice_mode")
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("voice-mode is not installed in this Python (use the repo's .venv)")
    return Path(list(spec.submodule_search_locations)[0])


def _tree(root: Path, skip=()) -> Path:
    """The installed voice_mode with pristine sources, patched by manifest.windows.json
    without the steps named in `skip`."""
    src = _src()
    pkg = root / "voice_mode"
    shutil.copytree(_installed_voice_mode(), pkg, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, name in SOURCES.items():
        shutil.copyfile(src / name, pkg / rel)
    manifest = json.loads((PATCHES / "manifest.windows.json").read_text(encoding="utf-8"))
    manifest["steps"] = [s for s in manifest["steps"] if (s.get("patch") or s.get("copy")) not in skip]
    listed = root / "manifest.json"
    listed.write_text(json.dumps(manifest), encoding="utf-8")
    r = subprocess.run([sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(pkg),
                        "--manifest", str(listed)], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONUTF8="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    return root


def _speak(tmp_path, root: Path, text: str, chunk_chars: int) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home),
        "PYTHONPATH": str(root), "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
        "VOICEMODE_BASE_DIR": str(home / ".voicemode"),
        "VOICEMODE_STREAMING_ENABLED": "true",
        "VOICEMODE_TTS_TIMEOUT_LOCAL": str(TIMEOUT_S),
        "VOICEMODE_TTS_CHUNK_CHARS": str(chunk_chars),
        "VOICEMODE_VOICES": "af_sky",
        "VOICEMODE_PILL": "false",
        "VOICEMODE_CONTROL_CHANNEL_ENABLED": "false",
        "VOICEMODE_MCP_URL": "",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    r = subprocess.run([sys.executable, str(driver), str(SECONDS_PER_CHAR)], input=text,
                       capture_output=True, text=True, env=env, timeout=120)
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, f"exit {r.returncode}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
    return json.loads(line[len("RESULT "):])


@pytest.fixture(scope="module")
def patched(tmp_path_factory):
    return _tree(tmp_path_factory.mktemp("patched"))


def test_the_long_reply_as_one_request_times_out(tmp_path, patched):
    """The field failure, reproduced: one request for the whole reply cannot finish."""
    result = _speak(tmp_path, patched, LONG, chunk_chars=0)
    assert not result["ok"]
    assert [r["chars"] for r in result["requests"]] == [len(LONG)]
    assert result["requests"][0]["seconds"] > TIMEOUT_S


def test_after_a_timeout_the_same_text_is_not_sent_again(tmp_path, patched):
    result = _speak(tmp_path, patched, LONG, chunk_chars=0)
    assert len(result["requests"]) == 1, result["requests"]
    assert len(result["errors"]) == 1
    error = result["errors"][0]
    assert error.startswith(f"Request timed out after {TIMEOUT_S:g} s.")
    assert "Not sent again without streaming" in error


def test_without_the_patch_the_same_text_is_sent_twice(tmp_path):
    """The control for the test above: upstream's fallback sends the identical text again
    on the buffered path, which is how the field turn waited 90 s + 90 s."""
    root = _tree(tmp_path / "unpatched", skip={"patch_tts_no_double_synthesis.py"})
    result = _speak(tmp_path, root, LONG, chunk_chars=0)
    assert [r["text"] for r in result["requests"]] == [LONG, LONG]
    assert [r["format"] for r in result["requests"]] == ["pcm", "pcm"]
    assert not result["ok"]


def test_a_long_reply_in_parts_is_spoken_and_every_part_ends_inside_the_timeout(tmp_path, patched):
    result = _speak(tmp_path, patched, LONG, chunk_chars=CHUNK_CHARS)
    assert result["ok"], result["errors"]
    parts = result["requests"]
    assert len(parts) >= 6, [p["chars"] for p in parts]
    assert all(p["chars"] <= CHUNK_CHARS for p in parts)
    assert all(p["seconds"] < TIMEOUT_S for p in parts), [p["seconds"] for p in parts]
    assert " ".join(p["text"] for p in parts).split() == LONG.split()


def test_a_short_reply_is_still_one_request(tmp_path, patched):
    result = _speak(tmp_path, patched, "Done. The build is green.", chunk_chars=CHUNK_CHARS)
    assert result["ok"]
    assert [r["text"] for r in result["requests"]] == ["Done. The build is green."]
