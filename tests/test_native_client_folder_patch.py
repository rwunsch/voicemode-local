"""patches/patch_native_client_folder.py: the session label's folder fallback uses the
client's folder, which the native launcher passes as VOICEMODE_CLIENT_FOLDER, because
voice-mode itself now runs in the runtime folder.

Run with: VM812_SRC=<dir holding pristine 8.12.0 converse.py>
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _converse(tmp_path, crlf=False):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set")
    target = tmp_path / "converse.py"
    data = (Path(raw) / "converse.py").read_bytes()
    target.write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)
    r = subprocess.run([sys.executable, str(PATCHES / "patch_session_name.py"), str(target)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return target


def _label_function(text):
    """The patched _vml_session_name, run on its own."""
    start = text.index("def _vml_session_name")
    import re
    nxt = re.compile(r"^\S", re.M).search(text, text.index("\n", start) + 1)
    end = nxt.start() if nxt else len(text)
    namespace = {}
    exec(text[start:end], namespace)
    return namespace["_vml_session_name"]


def test_label_uses_the_client_folder(tmp_path, monkeypatch):
    target = _converse(tmp_path)
    assert _load("patch_native_client_folder").apply(target) == 0
    label = _label_function(target.read_text())
    for name in ("VOICEMODE_SESSION_NAME", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VOICEMODE_CLIENT_FOLDER", str(tmp_path / "work" / "my-project"))
    assert label() == "my-project"
    monkeypatch.delenv("VOICEMODE_CLIENT_FOLDER")
    assert label() == tmp_path.name


@pytest.mark.parametrize("crlf", [False, True])
def test_idempotent_keeps_endings_and_refuses_a_changed_file(tmp_path, crlf):
    target = _converse(tmp_path, crlf)
    mod = _load("patch_native_client_folder")
    assert mod.apply(target) == 0
    once = target.read_bytes()
    assert mod.apply(target) == 0 and target.read_bytes() == once
    if crlf:
        assert b"\n" not in once.replace(b"\r\n", b"")
    other = tmp_path / "other.py"
    other.write_text("nothing here\n")
    assert mod.apply(other) == 1
