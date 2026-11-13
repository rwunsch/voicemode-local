"""vml_supervisor.winjob on real Windows: does a child spawned with breakaway leave its parent's job?

Run by tests/run_windows_transport_tests.sh. A helper Python is put in a "parent" job of ours
(KILL_ON_JOB_CLOSE, breakaway allowed). The helper starts a sleeping child, either through
winjob.spawn with a job of its own (breakaway asked for) or through a plain Popen (the control),
prints the child's pid and the Result code, and waits on stdin so its own job stays open. The
test then asks IsProcessInJob whether the child is in the parent job.
"""
import ctypes
import json
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

HELPER = r"""
import json, subprocess, sys
sys.path.insert(0, sys.argv[1])
from vml_supervisor import winjob
child = [sys.executable, "-c", "import time; time.sleep(60)"]
if sys.argv[2] == "spawn":
    own = winjob.Job()
    process, result = winjob.spawn(child, own, creationflags=subprocess.CREATE_NO_WINDOW)
    code = result.code
else:
    process = subprocess.Popen(child, creationflags=subprocess.CREATE_NO_WINDOW)
    code = "plain"
print(json.dumps({"pid": process.pid, "code": code}), flush=True)
sys.stdin.read()
process.kill()
"""

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _k32.CloseHandle.argtypes = (wintypes.HANDLE,)


def _in_job(pid, job_handle):
    process = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    assert process, ctypes.WinError(ctypes.get_last_error())
    try:
        inside = wintypes.BOOL()
        assert _k32.IsProcessInJob(process, job_handle, ctypes.byref(inside)), \
            ctypes.WinError(ctypes.get_last_error())
        return bool(inside.value)
    finally:
        _k32.CloseHandle(process)


def _run_helper(mode):
    from vml_supervisor import winjob
    parent = winjob.Job(breakaway=True)
    helper, placed = winjob.spawn([sys.executable, "-c", HELPER, str(REPO), mode], parent,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        report = json.loads(helper.stdout.readline())
        return placed, report, _in_job(report["pid"], parent.handle), _in_job(helper.pid, parent.handle)
    finally:
        helper.stdin.close()
        helper.wait(30)
        parent.close()


def test_a_child_spawned_with_breakaway_is_not_in_the_parents_job():
    placed, report, child_in_parent, helper_in_parent = _run_helper("spawn")
    print(f"[breakaway] helper placed: {placed.code}; child: {report}")
    assert helper_in_parent, "the helper must be in the parent job for the check to mean anything"
    assert report["code"] == "breakaway"
    assert not child_in_parent


def test_control_a_plain_child_stays_in_the_parents_job():
    placed, report, child_in_parent, helper_in_parent = _run_helper("plain")
    print(f"[control] helper placed: {placed.code}; child: {report}")
    assert helper_in_parent
    assert child_in_parent
