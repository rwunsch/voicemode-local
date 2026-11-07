"""patch_control_transport on pristine 8.12.0 control_socket.py.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_control_transport_patch.py

The patched control_socket.py is executed for real against a stub voice_mode
package (stub config and history buffer, the real 8.12.0 control_channel, the
real control_transport), so both the POSIX path and the no-AF_UNIX path run.
On Linux the pipe itself is replaced by a socketpair-backed fake transport.
"""
import importlib.util
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
SCRIPT = PATCHES / "patch_control_transport.py"
MARKER = "voicemode-local control transport"


def _src():
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    return Path(raw)


def _run(script, target):
    return subprocess.run([sys.executable, str(script), str(target)],
                          capture_output=True, text=True)


def _patch(script, target):
    r = _run(PATCHES / script, target)
    assert r.returncode == 0, f"{script} on {target.name}: {r.stderr}"
    return r.stdout


def _load_patch_module():
    spec = importlib.util.spec_from_file_location("vml_patch_ct", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def pristine(tmp_path):
    d = tmp_path / "pristine"
    d.mkdir()
    shutil.copy(_src() / "control_socket.py", d / "control_socket.py")
    shutil.copy(_src() / "control_channel.py", d / "control_channel.py")
    return d


@pytest.fixture()
def patched(pristine):
    _patch("patch_control_transport.py", pristine / "control_socket.py")
    return pristine


# --- anchors ----------------------------------------------------------------

def test_anchors_match_exactly_in_pristine(pristine):
    pm = _load_patch_module()
    src = (pristine / "control_socket.py").read_text()
    for name, anchor, _, expected in pm.EDITS:
        assert src.count(anchor) == expected, name


def test_anchors_match_after_existing_control_patches(pristine):
    """Same order as apply.sh: barge then pill touch control_socket.py first."""
    target = pristine / "control_socket.py"
    _patch("patch_voice_barge.py", target)
    _patch("patch_voice_pill.py", target)
    pm = _load_patch_module()
    src = target.read_text()
    for name, anchor, _, expected in pm.EDITS:
        assert src.count(anchor) == expected, name
    out = _patch("patch_control_transport.py", target)
    assert "patched" in out and MARKER in target.read_text()


def test_existing_control_patches_still_apply_after_it(patched):
    target = patched / "control_socket.py"
    _patch("patch_voice_barge.py", target)
    _patch("patch_voice_pill.py", target)


def test_idempotent(patched):
    target = patched / "control_socket.py"
    before = target.read_text()
    r = _run(SCRIPT, target)
    assert r.returncode == 0 and "already patched" in r.stdout
    assert target.read_text() == before


def test_drift_fails_loudly_and_writes_nothing(pristine):
    target = pristine / "control_socket.py"
    src = target.read_text().replace(
        "    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:\n", "    pass\n", 1)
    target.write_text(src)
    r = _run(SCRIPT, target)
    assert r.returncode != 0
    assert "ANCHOR DRIFT" in r.stderr
    assert target.read_text() == src


def test_patched_source_compiles(patched):
    compile((patched / "control_socket.py").read_text(), "control_socket.py", "exec")


# --- executing the patched module --------------------------------------------

class _History:
    maxlen = 8

    def snapshot(self):
        return []


class _Poison(types.ModuleType):
    """A control_transport that fails the test if the POSIX path touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"POSIX path touched control_transport.{name}")


def _load_control_socket(directory, monkeypatch, transport, enabled=False, sock=None):
    pkg = types.ModuleType("voice_mode")
    pkg.__path__ = []
    config = types.ModuleType("voice_mode.config")
    config.CONTROL_SOCKET_PATH = str(sock) if sock else str(directory / "control.sock")
    config.CONTROL_CHANNEL_ENABLED = enabled
    hb = types.ModuleType("voice_mode.history_buffer")
    hb.HistoryBuffer = _History
    hb.get_history_buffer = lambda: _History()
    spec = importlib.util.spec_from_file_location("voice_mode.control_channel",
                                                  directory / "control_channel.py")
    cc = importlib.util.module_from_spec(spec)
    for name, mod in (("voice_mode", pkg), ("voice_mode.config", config),
                      ("voice_mode.history_buffer", hb), ("voice_mode.control_channel", cc)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(cc)
    pkg.config, pkg.history_buffer, pkg.control_channel = config, hb, cc
    if transport is not None:
        monkeypatch.setitem(sys.modules, "voice_mode.control_transport", transport)
        pkg.control_transport = transport
    else:
        # Make the import fail, as on an install that never copied the module.
        monkeypatch.setitem(sys.modules, "voice_mode.control_transport", None)
    spec = importlib.util.spec_from_file_location("voice_mode.control_socket",
                                                  directory / "control_socket.py")
    cs = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "voice_mode.control_socket", cs)
    spec.loader.exec_module(cs)
    return cs, cc


def _short_sock_dir():
    # AF_UNIX paths are capped near 108 bytes; pytest tmp paths can exceed it.
    import tempfile
    return Path(tempfile.mkdtemp(prefix="vmlct-"))


def test_posix_path_is_unchanged_and_never_touches_the_transport(patched, monkeypatch):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("POSIX only")
    sd = _short_sock_dir()
    try:
        sock = sd / "control.sock"
        cs, cc = _load_control_socket(patched, monkeypatch, _Poison("voice_mode.control_transport"),
                                      sock=sock)
        state = cc.ControlState()
        listener = cs.ControlSocketListener(socket_path=sock, control_state=state,
                                            history_buffer=_History())
        listener.start()
        try:
            assert sock.exists()
            reply = cs.query_status(socket_path=sock, request_id=7)
            assert reply["request_id"] == 7 and reply["state"] == "running"
            cs.send_control_command("stop", socket_path=sock)
            _wait(lambda: state.snapshot().is_stopped)
        finally:
            listener.stop()
        assert not sock.exists()
        with pytest.raises(FileNotFoundError):
            cs.send_control_command("stop", socket_path=sock)
    finally:
        shutil.rmtree(sd, ignore_errors=True)


class FakePipeTransport(types.ModuleType):
    """Stands in for the Windows pipe: one queue per server, socketpair conns."""

    def __init__(self):
        super().__init__("voice_mode.control_transport")
        self.servers = {}
        self.created, self.connected = [], []

    def create_server(self, path):
        self.created.append(Path(path))
        q = queue.Queue()
        self.servers[str(Path(path))] = q
        transport = self

        class Server:
            timeout = None

            def settimeout(self, t):
                self.timeout = t

            def accept(self):
                try:
                    return q.get(timeout=self.timeout), None
                except queue.Empty:
                    raise socket.timeout("timed out")

            def close(self):
                transport.servers.pop(str(Path(path)), None)

        return Server()

    def connect(self, path, timeout):
        self.connected.append(Path(path))
        q = self.servers.get(str(Path(path)))
        if q is None:
            raise FileNotFoundError(2, "no pipe", str(path))
        import _socket  # the fake's own plumbing; AF_UNIX is hidden from `socket`
        a, b = socket.socketpair(getattr(_socket, "AF_UNIX", socket.AF_INET))
        a.settimeout(timeout)
        q.put(b)
        return a


def test_missing_af_unix_selects_the_pipe_on_both_ends(patched, monkeypatch, tmp_path):
    fake = FakePipeTransport()
    cs, cc = _load_control_socket(patched, monkeypatch, fake)
    monkeypatch.delattr(socket, "AF_UNIX")
    monkeypatch.setattr(cs, "_peer_uid", lambda conn: None)  # what win32 returns
    path = tmp_path / "control.sock"
    state = cc.ControlState()
    listener = cs.ControlSocketListener(socket_path=path, control_state=state,
                                        history_buffer=_History())
    listener.start()
    try:
        assert fake.created == [path]
        reply = cs.query_status(socket_path=path, request_id="r1")
        assert reply["request_id"] == "r1" and reply["state"] == "running"
        cs.send_control_command("stop", socket_path=path)
        _wait(lambda: state.snapshot().is_stopped)
        assert fake.connected == [path, path]
    finally:
        listener.stop()
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        cs.send_control_command("stop", socket_path=path)


def test_enabled_without_af_unix_unpatched_loses_the_turn(pristine, monkeypatch):
    """Documents the bug the patch exists for: pristine raises AttributeError,
    which converse does not catch, so the turn is lost."""
    cs, _ = _load_control_socket(pristine, monkeypatch, FakePipeTransport(), enabled=True)
    monkeypatch.delattr(socket, "AF_UNIX")
    with pytest.raises(AttributeError):
        cs.start_control_listener()


def test_enabled_without_af_unix_patched_never_raises(patched, monkeypatch):
    """Fails if the control channel is enabled on a no-AF_UNIX platform without
    this patch: start_control_listener must never raise out of a converse turn."""
    fake = FakePipeTransport()
    cs, _ = _load_control_socket(patched, monkeypatch, fake, enabled=True)
    monkeypatch.delattr(socket, "AF_UNIX")
    monkeypatch.setattr(cs, "_peer_uid", lambda conn: None)
    listener = cs.start_control_listener()
    try:
        assert listener is not None and listener.is_running
    finally:
        cs.stop_control_listener()


def test_pipe_bind_failure_is_non_fatal(patched, monkeypatch):
    fake = FakePipeTransport()

    def refuse(path):
        raise PermissionError(5, "Access is denied", str(path))
    fake.create_server = refuse
    cs, _ = _load_control_socket(patched, monkeypatch, fake, enabled=True)
    monkeypatch.delattr(socket, "AF_UNIX")
    assert cs.start_control_listener() is None


def test_missing_transport_module_is_non_fatal(patched, monkeypatch):
    cs, _ = _load_control_socket(patched, monkeypatch, None, enabled=True)
    monkeypatch.delattr(socket, "AF_UNIX")
    assert cs.start_control_listener() is None
    with pytest.raises(OSError):
        cs.send_control_command("stop")


def test_manifest_installs_the_module_before_running_the_patch():
    run = _manifest_index(patch="patch_control_transport.py", targets="control_socket.py")
    assert _manifest_index(copy="control_transport.py") < run


def _manifest_index(**match):
    """Position of the first manifest step whose fields contain ``match``."""
    import json
    steps = json.loads((PATCHES / "manifest.json").read_text(encoding="utf-8"))["steps"]
    for i, step in enumerate(steps):
        if all(v in step.get(k, ()) if isinstance(step.get(k), list) else step.get(k) == v
               for k, v in match.items()):
            return i
    raise AssertionError(f"no manifest step with {match}")


def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")
