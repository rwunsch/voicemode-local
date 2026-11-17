"""Tests for whisper-proxy.py endpoints.

Mirrors the structure of test_piper_proxy.py — starts the proxy as a
subprocess and exercises the GET endpoints. POST /v1/audio/transcriptions
requires the Whisper backend, so only side-effect-free endpoints are tested.
"""

import http.server
import json
import os
import subprocess
import threading
import sys
import time
import urllib.request
import urllib.error
import pathlib

PROXY_SCRIPT = str(pathlib.Path(__file__).parent.parent / "whisper-proxy.py")
TEST_PORT = 12022
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


DEAD_BACKEND = "http://127.0.0.1:19999"   # nothing listens here


class FakeBackend:
    """A stand-in for the whisper backend: answers every GET with 200."""

    def __init__(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


NO_CONFIG = "/nonexistent/voicemode-local-config"


def _env(config_path=NO_CONFIG):
    """Never read the real ~/.voicemode-local/config: on a faster-whisper machine the
    startup guard would make every test's proxy exit."""
    return {**os.environ, "VOICEMODE_LOCAL_CONFIG": str(config_path)}


def _start_proxy(whisper_url=DEAD_BACKEND, config_path=NO_CONFIG, extra=()):
    proc = subprocess.Popen(
        [
            sys.executable,
            PROXY_SCRIPT,
            "--port", str(TEST_PORT),
            "--whisper-url", whisper_url,
            *extra,
        ],
        env=_env(config_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE_URL}/health", timeout=3)
            return proc
        except urllib.error.HTTPError:
            return proc   # 503 backend_unreachable still means the proxy listens
        except (urllib.error.URLError, ConnectionRefusedError):
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"whisper-proxy did not start within 5 seconds on port {TEST_PORT}")


def _stop_proxy(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _get(path):
    req = urllib.request.Request(f"{BASE_URL}{path}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


def _post(path, body=None, content_type="application/json"):
    data = json.dumps(body).encode() if body else b""
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── GET endpoints ─────────────────────────────────────────────────────────


def test_health_ok_when_the_backend_answers():
    backend = FakeBackend()
    proc = _start_proxy(backend.url)
    try:
        status, body = _get("/health")
        assert status == 200
        assert body == {"status": "ok", "engine": "whisper-proxy", "backend": "ok"}
    finally:
        _stop_proxy(proc)
        backend.stop()


def test_health_503_when_the_backend_is_down():
    """The 2026-11-17 failure: the proxy answered 200 while its backend was dead."""
    proc = _start_proxy(DEAD_BACKEND)
    try:
        status, body = _get("/health")
        assert status == 503
        assert body == {"status": "backend_unreachable", "backend": DEAD_BACKEND}
    finally:
        _stop_proxy(proc)


def test_health_follows_the_backend_going_down():
    backend = FakeBackend()
    proc = _start_proxy(backend.url)
    try:
        assert _get("/health")[0] == 200
        backend.stop()
        time.sleep(2.5)   # the probe result is cached for about 2 s
        assert _get("/health")[0] == 503
    finally:
        _stop_proxy(proc)


def test_health_probe_is_cached_for_a_burst():
    """A probe storm costs one backend request, not one per /health."""
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(1)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = _start_proxy(f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        before = len(hits)
        for _ in range(10):
            assert _get("/health")[0] == 200
        assert len(hits) - before <= 1
    finally:
        _stop_proxy(proc)
        srv.shutdown()
        srv.server_close()


def test_models_endpoint():
    proc = _start_proxy()
    try:
        status, body = _get("/v1/models")
        assert status == 200
        assert body["object"] == "list"
        ids = [m["id"] for m in body["data"]]
        assert "whisper-1" in ids
    finally:
        _stop_proxy(proc)


def test_404_for_unknown_get():
    proc = _start_proxy()
    try:
        status, _ = _get("/nonexistent")
        assert status == 404
    finally:
        _stop_proxy(proc)


# ── POST endpoint error paths ────────────────────────────────────────────


def test_404_for_unknown_post():
    proc = _start_proxy()
    try:
        status, _ = _post("/nonexistent")
        assert status == 404
    finally:
        _stop_proxy(proc)


def test_transcription_rejects_non_multipart():
    """POST /v1/audio/transcriptions without multipart boundary returns 400."""
    proc = _start_proxy()
    try:
        status, _ = _post("/v1/audio/transcriptions", body={"file": "test"})
        assert status == 400
    finally:
        _stop_proxy(proc)


# ── startup guard: STT_ENGINE=faster-whisper must not let this proxy take 2022 ──

GUARD_LINE = "STT_ENGINE=faster-whisper: not starting the Docker proxy; run voicemode-switch ensure"


def _run_guarded(tmp_path, config_text, *extra):
    cfg = tmp_path / "config"
    if config_text is not None:
        cfg.write_text(config_text)
    return subprocess.run(
        [sys.executable, PROXY_SCRIPT, "--port", str(TEST_PORT), "--whisper-url", DEAD_BACKEND, *extra],
        env=_env(cfg), capture_output=True, text=True, timeout=5,
    )


def test_guard_exits_0_without_binding_when_the_engine_is_faster_whisper(tmp_path):
    done = _run_guarded(tmp_path, "KOKORO_ONNX=false\nSTT_ENGINE=faster-whisper\n")
    assert done.returncode == 0
    assert done.stderr.strip() == GUARD_LINE
    assert done.stdout == ""
    # it never bound the port
    try:
        urllib.request.urlopen(f"{BASE_URL}/health", timeout=1)
        raise AssertionError("something listens on the test port")
    except urllib.error.URLError:
        pass


def test_guard_last_config_line_wins_like_vm_read_config(tmp_path):
    done = _run_guarded(tmp_path, "STT_ENGINE=faster-whisper\nSTT_ENGINE=docker\n", "--help")
    assert GUARD_LINE not in done.stderr   # docker wins: normal startup path (--help exits)


def test_guard_serves_when_the_engine_is_docker(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("STT_ENGINE=docker\n")
    proc = _start_proxy(config_path=cfg)
    try:
        assert _get("/health")[0] == 503   # listening (backend is the dead one)
    finally:
        _stop_proxy(proc)


def test_guard_serves_when_the_config_is_missing_or_the_engine_unset(tmp_path):
    proc = _start_proxy()   # config path does not exist
    try:
        assert _get("/health")[0] == 503
    finally:
        _stop_proxy(proc)
    cfg = tmp_path / "config"
    cfg.write_text("KOKORO_ONNX=false\n")
    proc = _start_proxy(config_path=cfg)
    try:
        assert _get("/health")[0] == 503
    finally:
        _stop_proxy(proc)


def test_force_serves_even_when_the_engine_is_faster_whisper(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("STT_ENGINE=faster-whisper\n")
    proc = _start_proxy(config_path=cfg, extra=("--force",))
    try:
        assert _get("/health")[0] == 503
    finally:
        _stop_proxy(proc)
