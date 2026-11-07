"""The native Windows patch list: patches/manifest.windows.json, `apply.py --native-windows`.

Native Windows has no ffmpeg, so it adds FELIX's three patches (no_ffmpeg_gate,
no_ffmpeg_warning, load_scipy_early) in FELIX's order relative to ours. WSL and Mac
keep ffmpeg and patches/manifest.json, whose output must not change by one byte.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources, server.py included>
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
BEFORE = "d30ec68"  # the last commit before the native list existed
FELIX_THREE = ["patch_no_ffmpeg_gate.py", "patch_no_ffmpeg_warning.py", "patch_load_scipy_early.py"]
# Ours, native only: Windows cannot delete an open temp file (WinError 32). Runs last.
NATIVE_OURS = ["patch_windows_tempfile.py", "patch_native_client_folder.py"]
SOURCES = {
    "control_channel.py": "control_channel.py", "control_socket.py": "control_socket.py",
    "core.py": "core.py", "simple_failover.py": "simple_failover.py", "streaming.py": "streaming.py",
    "server.py": "server.py", "tools/converse.py": "converse.py",
}


def _src():
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    src = Path(raw)
    for name in SOURCES.values():
        if not (src / name).is_file():
            pytest.fail(f"VM812_SRC={src} has no {name}")
    return src


def _tree(root, crlf=False):
    pkg = root / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    for rel, name in SOURCES.items():
        data = (_src() / name).read_bytes()
        (pkg / rel).write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)
    return pkg


def _apply(apply_py, pkg, *extra):
    r = subprocess.run([sys.executable, str(apply_py), "--package-dir", str(pkg), *extra],
                       capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def _files(pkg):
    return {p.relative_to(pkg).as_posix(): p.read_bytes()
            for p in sorted(pkg.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def _patches_at(commit, dest):
    data = subprocess.run(["git", "-C", str(REPO), "archive", commit, "patches"],
                          capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(dest, filter="data")
    return current_modules(dest / "patches")


def current_modules(old):
    """Lay today's copied modules (manifest.json "copy" steps) over an old patches/ tree.
    These byte tests check the patching mechanism; a module's content changes by design
    (main's 5a6cb8d changed voice_pill_state.py) and is covered by its own tests."""
    steps = json.loads((PATCHES / "manifest.json").read_text(encoding="utf-8"))["steps"]
    for name in {s["copy"] for s in steps if "copy" in s}:
        shutil.copyfile(PATCHES / name, old / name)
    return old


def _steps(name):
    return json.loads((PATCHES / name).read_text(encoding="utf-8"))["steps"]


# -- WSL and Mac: unchanged but for the patches added on purpose ------------

# Steps added to manifest.json since BEFORE, each by design; the rest stays byte-identical.
INTENDED_STEP_LINES = [
    b'    {"patch": "patch_ptt_instant.py",\n'
    b'     "targets": ["control_channel.py", "control_socket.py", "tools/converse.py"],\n'
    b'     "why": "push-to-talk cuts TTS generation, records at once, a stop clears the hold; '
    b'needs hold, barge and pill"},\n',
]
INTENDED_PATCHES = [("patch_ptt_instant.py", ["control_channel.py", "control_socket.py", "tools/converse.py"])]
# 2026-11-07: session_wake.py, copied in after voice_pill_state.py.
INTENDED_STEP_LINES.append(
    b'    {"copy": "session_wake.py", "to": "session_wake.py", "why": "wake an idle Claude Code '
    b'session: push-to-talk hold and the pill\'s Play"},\n')
INTENDED_COPIES = ["session_wake.py"]


def test_default_manifest_file_is_unchanged():
    old = subprocess.run(["git", "-C", str(REPO), "show", f"{BEFORE}:patches/manifest.json"],
                         capture_output=True, check=True).stdout
    now = (PATCHES / "manifest.json").read_bytes()
    for lines in INTENDED_STEP_LINES:
        assert now.count(lines) == 1
        now = now.replace(lines, b"")
    assert now == old


def test_default_output_is_byte_identical_to_before(tmp_path):
    old = _patches_at(BEFORE, tmp_path / "old")
    before, now = _tree(tmp_path / "a"), _tree(tmp_path / "b")
    _apply(old / "apply.py", before)
    for script, targets in INTENDED_PATCHES:
        for rel in targets:
            r = subprocess.run([sys.executable, str(PATCHES / script), str(before / rel)],
                               capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
            assert r.returncode == 0, r.stdout + r.stderr
    for name in INTENDED_COPIES:
        shutil.copy(PATCHES / name, before / name)
    _apply(PATCHES / "apply.py", now)
    assert _files(now) == _files(before)
    assert "FELIX: NO FFMPEG" not in (now / "tools/converse.py").read_text(encoding="utf-8")


# -- native Windows ---------------------------------------------------------

def test_native_list_is_the_default_plus_felix_three_in_felixs_place():
    default, native = _steps("manifest.json"), _steps("manifest.windows.json")
    added = [s for s in native if s.get("patch") in FELIX_THREE]
    assert [s["patch"] for s in added] == FELIX_THREE
    ours = [s for s in native if s.get("patch") in NATIVE_OURS]
    assert native[-len(ours):] == ours and [s["patch"] for s in ours] == NATIVE_OURS
    assert ours[0]["targets"] == ["core.py", "streaming.py"]
    assert ours[1]["targets"] == ["tools/converse.py"]
    assert [s for s in native if s not in added and s not in ours] == default
    # FELIX's order: listen_overrun, turns_playback_stop, simple_failover, then its three.
    names = [s.get("patch") or s.get("copy") for s in native]
    at = names.index("patch_simple_failover.py")
    assert names[at + 1:at + 4] == FELIX_THREE
    targets = {s["patch"]: s["targets"] for s in added}
    assert targets == {"patch_no_ffmpeg_gate.py": ["tools/converse.py"],
                       "patch_no_ffmpeg_warning.py": ["server.py"],
                       "patch_load_scipy_early.py": ["tools/converse.py"]}
    unshare = json.loads((PATCHES / "manifest.windows.json").read_text(encoding="utf-8"))["unshare"]
    assert "server.py" in unshare


@pytest.mark.parametrize("crlf", [False, True], ids=["LF", "CRLF"])
def test_native_list_applies_cleanly_and_is_idempotent(tmp_path, crlf):
    pkg = _tree(tmp_path, crlf=crlf)
    _apply(PATCHES / "apply.py", pkg, "--native-windows")
    once = _files(pkg)
    _apply(PATCHES / "apply.py", pkg, "--native-windows")
    assert _files(pkg) == once
    converse = once["tools/converse.py"].decode("utf-8")
    assert "FELIX: NO FFMPEG" in converse and "FELIX: LOAD SCIPY EARLY" in converse
    assert "FELIX: NO FFMPEG WARNING" in once["server.py"].decode("utf-8")
    assert "VML: WINDOWS TEMPFILE" in once["core.py"].decode("utf-8")
    assert "VML: WINDOWS TEMPFILE" in once["streaming.py"].decode("utf-8")
    for rel in SOURCES:
        data = once[rel]
        if crlf:
            assert b"\n" not in data.replace(b"\r\n", b""), f"{rel} lost its CRLF endings"
        else:
            assert b"\r\n" not in data, rel


def test_native_output_differs_from_default_only_by_felixs_three_and_the_tempfile_fix(tmp_path):
    default, native = _tree(tmp_path / "d"), _tree(tmp_path / "n")
    _apply(PATCHES / "apply.py", default)
    _apply(PATCHES / "apply.py", native, "--native-windows")
    d, n = _files(default), _files(native)
    assert set(d) == set(n)
    assert {rel for rel in d if d[rel] != n[rel]} == {"tools/converse.py", "server.py", "core.py", "streaming.py"}
    assert "VML: WINDOWS TEMPFILE" not in d["core.py"].decode("utf-8")
    assert "os.unlink(tmp_file.name)" not in n["core.py"].decode("utf-8")


@pytest.mark.parametrize("script,rel", [("patch_no_ffmpeg_gate.py", "tools/converse.py"),
                                        ("patch_no_ffmpeg_warning.py", "server.py"),
                                        ("patch_load_scipy_early.py", "tools/converse.py")])
def test_our_copies_keep_crlf(tmp_path, script, rel):
    """The case test_patch_trees marks xfail for FELIX's own scripts, which write LF."""
    pkg = _tree(tmp_path, crlf=True)
    r = subprocess.run([sys.executable, str(PATCHES / script), str(pkg / rel)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = (pkg / rel).read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_our_copies_record_provenance():
    for name in FELIX_THREE:
        text = (PATCHES / name).read_text(encoding="utf-8")
        assert "felix-framework" in text and "aea841ffd" in text, name


# -- the ffmpeg check in converse, with no ffmpeg on PATH --------------------

GATE_START = "ffmpeg_available = "
GATE_END = 'return f"❌ Error: {error_msg}"'


def _gate(converse_text):
    """converse's ffmpeg check, lifted out of the function into one we can call."""
    lines = converse_text.replace("\r\n", "\n").split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip().startswith(GATE_START))
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == GATE_END)
    body = textwrap.dedent("\n".join(lines[start:end + 1]))
    body = body.replace("from ..utils.ffmpeg_check import get_install_instructions",
                        "get_install_instructions = lambda: 'install ffmpeg'")
    return "def gate():\n" + textwrap.indent(body, "    ") + "\n    return 'passed'\n"


def _run_gate(converse_text, no_ffmpeg_path, monkeypatch):
    """With PATH holding no ffmpeg: what server.py would set, then converse's check."""
    monkeypatch.setenv("PATH", str(no_ffmpeg_path))
    available = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
    config = type("config", (), {"FFMPEG_AVAILABLE": available})
    namespace = {"voice_mode": type("voice_mode", (), {"config": config}),
                 "logger": type("logger", (), {"error": staticmethod(lambda *a: None)})}
    exec(_gate(converse_text), namespace)
    return available, namespace["gate"]()


def test_ffmpeg_check_passes_without_ffmpeg_on_path_natively(tmp_path, monkeypatch):
    empty = tmp_path / "bin"
    empty.mkdir()
    pristine = (_src() / "converse.py").read_text(encoding="utf-8")
    available, result = _run_gate(pristine, empty, monkeypatch)
    assert available is False                     # the simulated PATH really has no ffmpeg
    assert result.startswith("❌ Error: FFmpeg is required")  # and pristine refuses

    pkg = _tree(tmp_path / "native")
    _apply(PATCHES / "apply.py", pkg, "--native-windows")
    native = (pkg / "tools/converse.py").read_text(encoding="utf-8")
    available, result = _run_gate(native, empty, monkeypatch)
    assert available is False and result == "passed"


@pytest.mark.parametrize("name", FELIX_THREE)
def test_our_copies_fail_loudly_on_drift_and_write_nothing(tmp_path, name):
    target = tmp_path / "target.py"
    target.write_bytes(b"x = 1\r\n")
    r = subprocess.run([sys.executable, str(PATCHES / name), str(target)], capture_output=True, text=True)
    assert r.returncode != 0 and r.stderr.strip()
    assert target.read_bytes() == b"x = 1\r\n"
