"""patches/patch_windows_tempfile.py: never delete a temp audio file while it is still open.

voice-mode 8.12.0 (and 9.0.2) deletes the TTS temp file inside the `with` that holds it
open. Linux allows that; Windows refuses with WinError 32, and the error reaches the
caller as "TTS service connection failed", after the audio has already played. Seen
2026-11-06 in the first spoken test of native Windows mode: 3 of 3 turns failed (n=1 run).

Run with: VM812_SRC=<dir holding pristine 8.12.0 core.py and streaming.py>
"""
import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCH = REPO / "patches" / "patch_windows_tempfile.py"


def _patch():
    spec = importlib.util.spec_from_file_location("patch_windows_tempfile", PATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _copy(tmp_path, name, crlf=False):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    data = (Path(raw) / name).read_bytes()
    target = tmp_path / name
    target.write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)
    return target


def test_core_no_unlink_of_an_open_temp_file(tmp_path):
    core = _copy(tmp_path, "core.py")
    assert _patch().apply(core) == 0
    text = core.read_text()
    assert "os.unlink(tmp_file.name)" not in text
    assert text.count("_vml_close_unlink(tmp_file)") == 6
    compile(text, str(core), "exec")


def test_streaming_closes_before_unlink(tmp_path):
    streaming = _copy(tmp_path, "streaming.py")
    assert _patch().apply(streaming) == 0
    text = streaming.read_text()
    assert text.count("tmp_wav.close(); os.unlink(tmp_wav.name)") == 2
    compile(text, str(streaming), "exec")


@pytest.mark.parametrize("name", ["core.py", "streaming.py"])
def test_idempotent_and_keeps_crlf(tmp_path, name):
    target = _copy(tmp_path, name, crlf=True)
    mod = _patch()
    assert mod.apply(target) == 0
    once = target.read_bytes()
    assert mod.apply(target) == 0
    assert target.read_bytes() == once
    assert b"\n" not in once.replace(b"\r\n", b"")


def test_refuses_when_voice_mode_changed(tmp_path):
    core = _copy(tmp_path, "core.py")
    core.write_text(core.read_text().replace("os.unlink(tmp_file.name)", "os.remove(tmp_file.name)", 1))
    before = core.read_bytes()
    assert _patch().apply(core) == 1
    assert core.read_bytes() == before


def test_helper_closes_before_it_deletes(monkeypatch):
    """Windows semantics on any OS: deleting a file that is still open fails."""
    namespace = {"os": os}
    exec(_patch().HELPER, namespace)
    real_unlink = os.unlink
    handle = tempfile.NamedTemporaryFile(suffix=".pcm", delete=False)
    handle.write(b"\0\0")

    def windows_unlink(path):
        if not handle.closed:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        real_unlink(path)

    monkeypatch.setattr(os, "unlink", windows_unlink)
    namespace["_vml_close_unlink"](handle)
    assert handle.closed
    assert not Path(handle.name).exists()


def test_helper_never_raises_on_a_failed_delete(monkeypatch, tmp_path):
    """Playback already happened: a file that cannot be deleted must not turn it into a failure."""
    namespace = {"os": os}
    exec(_patch().HELPER, namespace)
    handle = open(tmp_path / "x.pcm", "wb")

    def refuse(path):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(os, "unlink", refuse)
    namespace["_vml_close_unlink"](handle)
    assert handle.closed
