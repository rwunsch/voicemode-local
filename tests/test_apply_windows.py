"""patches/apply.py under native Windows Python, on scratch copies of pristine files.

Run by tests/run_windows_transport_tests.sh, which puts pristine 8.12.0 sources in
VML_APPLY_SRC and, in VML_APPLY_EXPECTED, the voice_mode tree Linux's apply.py made
from them. Windows must produce the same bytes from LF input, the same bytes in CRLF
from CRLF input, and change nothing on a second run.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")

PATCHES = Path(__file__).resolve().parent.parent / "patches"
SOURCES = {
    "control_channel.py": "control_channel.py",
    "control_socket.py": "control_socket.py",
    "core.py": "core.py",
    "simple_failover.py": "simple_failover.py",
    "streaming.py": "streaming.py",
    "tools/converse.py": "converse.py",
    "conch_notify.py": "conch_notify.py",
}


def _dirs():
    src, expected = os.environ.get("VML_APPLY_SRC"), os.environ.get("VML_APPLY_EXPECTED")
    if not src or not expected:
        pytest.skip("VML_APPLY_SRC / VML_APPLY_EXPECTED not set (see run_windows_transport_tests.sh)")
    return Path(src), Path(expected)


def _tree(root: Path, src: Path, crlf: bool) -> Path:
    pkg = root / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    for rel, name in SOURCES.items():
        data = (src / name).read_bytes()
        (pkg / rel).write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)
    return pkg


def _apply(pkg: Path, utf8: bool):
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)
    if utf8:
        env["PYTHONUTF8"] = "1"
    r = subprocess.run([sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(pkg)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def _files(pkg: Path) -> dict:
    return {p.relative_to(pkg).as_posix(): p.read_bytes()
            for p in sorted(pkg.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


@pytest.mark.parametrize("utf8", [True, False], ids=["PYTHONUTF8=1", "locale"])
def test_lf_input_matches_linux(tmp_path, utf8):
    src, expected = _dirs()
    pkg = _tree(tmp_path, src, crlf=False)
    _apply(pkg, utf8)
    first = _files(pkg)
    assert first == _files(expected)
    _apply(pkg, utf8)
    assert _files(pkg) == first


def test_crlf_input_keeps_crlf(tmp_path):
    src, expected = _dirs()
    pkg = _tree(tmp_path, src, crlf=True)
    _apply(pkg, True)
    want = _files(expected)
    got = _files(pkg)
    for rel in SOURCES:
        assert got[rel] == want[rel].replace(b"\n", b"\r\n"), rel
    _apply(pkg, True)
    assert _files(pkg) == got
