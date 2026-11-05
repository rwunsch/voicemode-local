"""tests/conftest.py: a test skipped for want of VM812_SRC fails under a release check.

Runs pytest in a subprocess on a scratch folder holding a copy of conftest.py and one
test that skips the way the patch tests do, so the hook is seen from outside.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFTEST = Path(__file__).resolve().parent / "conftest.py"

SKIPPING_TEST = '''
import os, pytest

def test_needs_sources():
    if not os.environ.get("VM812_SRC"):
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")

def test_other_skip():
    pytest.skip("needs Windows")
'''


def _run(tmp_path, **env):
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_sample.py").write_text(SKIPPING_TEST, encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if k not in ("VM812_SRC", "VOICEMODE_RELEASE_CHECK")}
    clean.update(env)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           f"--rootdir={tmp_path}", str(tmp_path)],
                          capture_output=True, text=True, env=clean, cwd=tmp_path)


def test_release_check_fails_when_vm812_src_missing(tmp_path):
    result = _run(tmp_path, VOICEMODE_RELEASE_CHECK="1")
    assert result.returncode != 0, result.stdout
    assert "VOICEMODE_RELEASE_CHECK=1 but VM812_SRC is not set" in result.stdout
    assert "1 failed" in result.stdout and "1 skipped" in result.stdout, result.stdout


def test_without_release_check_skips_with_a_loud_warning(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "2 skipped" in result.stdout
    assert "WARNING: 1 test(s) skipped because VM812_SRC is not set" in result.stdout


def test_no_warning_when_nothing_needed_it(tmp_path):
    result = _run(tmp_path, VM812_SRC=str(tmp_path))
    assert result.returncode == 0, result.stdout
    assert "VM812_SRC is not set" not in result.stdout
