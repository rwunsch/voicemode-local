"""Tests for vml_supervisor.winjob on every platform: the Windows branch is driven by patching the
module's WINDOWS flag and replacing subprocess.Popen and kernel32 with fakes."""
import sys
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from test_supervisor_profile import make_profile  # noqa: E402

from vml_supervisor import winjob  # noqa: E402


class FakeJob:
    def __init__(self, fail=False):
        self.assigned, self.fail = [], fail

    def assign(self, handle):
        if self.fail:
            raise OSError("access denied")
        self.assigned.append(handle)


class FakePipe:
    closed = False

    def close(self):
        self.closed = True


class FakePopen:
    """Records every creation; raises PermissionError for the flags in `refuse`."""
    calls, killed, waited, refuse, last = [], [], [], 0, None

    def __init__(self, argv, **kwargs):
        flags = kwargs.get("creationflags", 0)
        FakePopen.calls.append((argv, kwargs))
        if FakePopen.refuse and flags & FakePopen.refuse:
            raise PermissionError(5, "Access is denied")
        self.pid, self._handle = 4242, 7
        self.stdin, self.stdout, self.stderr = FakePipe(), FakePipe(), None
        self.pipes = [self.stdin, self.stdout]
        FakePopen.last = self

    def kill(self):
        FakePopen.killed.append(self.pid)

    def wait(self, timeout=None):
        FakePopen.waited.append((self.pid, timeout))
        return 1


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(winjob, "WINDOWS", True)
    monkeypatch.setattr(winjob.subprocess, "Popen", FakePopen)
    resumed = []
    monkeypatch.setattr(winjob, "_resume_threads", resumed.append)
    FakePopen.calls, FakePopen.killed, FakePopen.waited, FakePopen.refuse = [], [], [], 0
    return resumed


def test_breakaway_is_asked_for_and_recorded(windows):
    job = FakeJob()
    process, result = winjob.spawn(["x.exe"], job, creationflags=0x08000000, cwd="C:\\w")
    assert result.code == "breakaway"
    (argv, kwargs), = FakePopen.calls
    assert kwargs["creationflags"] == 0x08000000 | 0x4 | winjob.CREATE_BREAKAWAY_FROM_JOB
    assert kwargs["cwd"] == "C:\\w"
    assert job.assigned == [7] and windows == [4242]


def test_breakaway_refused_is_recorded(windows):
    FakePopen.refuse = winjob.CREATE_BREAKAWAY_FROM_JOB
    job = FakeJob()
    process, result = winjob.spawn(["x.exe"], job, creationflags=0x08000000)
    assert result.code == "breakaway_refused"
    assert "parent" in result.detail and "job" in result.detail
    first, second = (kw["creationflags"] for _, kw in FakePopen.calls)
    assert first & winjob.CREATE_BREAKAWAY_FROM_JOB
    assert second == 0x08000000 | 0x4  # retried without breakaway, still suspended, flags kept
    assert job.assigned == [7] and windows == [4242]  # still assigned to our job, then resumed


def test_a_failed_assign_kills_and_reports(windows):
    with pytest.raises(winjob.SpawnError) as caught:
        winjob.spawn(["x.exe"], FakeJob(fail=True))
    assert caught.value.result.code == "job_assign_failed"
    assert "access denied" in caught.value.result.detail
    assert FakePopen.killed == [4242]
    assert windows == []  # never resumed: it ran no instruction outside our job


def test_a_failed_resume_kills_and_reports(windows, monkeypatch):
    def refuse(pid):
        raise RuntimeError(f"could not resume process {pid}")
    monkeypatch.setattr(winjob, "_resume_threads", refuse)
    with pytest.raises(winjob.SpawnError) as caught:
        winjob.spawn(["x.exe"], FakeJob())
    assert caught.value.result.code == "resume_failed"
    assert FakePopen.killed == [4242]


def test_no_job_is_a_plain_popen(windows, monkeypatch):
    process, result = winjob.spawn(["x.exe"], None, creationflags=0x08000000)
    assert result.code == "no_job"
    assert FakePopen.calls == [(["x.exe"], {"creationflags": 0x08000000})] and windows == []
    monkeypatch.setattr(winjob, "WINDOWS", False)
    FakePopen.calls = []
    process, result = winjob.spawn(["x"], FakeJob(), cwd="/w")
    assert result.code == "no_job" and FakePopen.calls == [(["x"], {"cwd": "/w"})]


class FakeKernel:
    """kernel32 with just the calls Job makes; records the limit flags it is given."""

    def __init__(self, create=11, set_ok=1, assign_ok=1):
        self.log, self.flags = [], None
        kernel = self

        class Fn:
            def __init__(self, name, impl):
                self.name, self.impl = name, impl

            def __call__(self, *args):
                kernel.log.append(self.name)
                return self.impl(*args)

        def set_info(handle, cls, pointer, size):
            kernel.flags = (cls, pointer._obj.BasicLimitInformation.LimitFlags)
            return set_ok

        self.CreateJobObjectW = Fn("create", lambda attrs, name: (kernel.log.append(name), create)[1])
        self.SetInformationJobObject = Fn("set", set_info)
        self.AssignProcessToJobObject = Fn("assign", lambda job, process: assign_ok)
        self.CloseHandle = Fn("close", lambda handle: 1)


@pytest.mark.parametrize("kwargs,flags", [({}, 0x2000), ({"breakaway": True}, 0x2800),
                                          ({"silent_breakaway": True}, 0x3800)])
def test_job_limit_flags(monkeypatch, kwargs, flags):
    kernel = FakeKernel()
    monkeypatch.setattr(winjob, "_kernel32", lambda: kernel)
    monkeypatch.setattr(winjob, "_last_error", lambda: OSError("fake"))
    job = winjob.Job("Local\\x", **kwargs)
    assert kernel.flags == (9, flags)  # JobObjectExtendedLimitInformation
    assert job.handle == 11
    job.assign(7)
    job.close()
    job.close()  # closing twice closes once
    assert kernel.log.count("close") == 1 and job.handle is None


def test_job_create_and_set_failures_raise_and_do_not_leak(monkeypatch):
    monkeypatch.setattr(winjob, "_last_error", lambda: OSError("fake"))
    monkeypatch.setattr(winjob, "_kernel32", lambda: FakeKernel(create=0))
    with pytest.raises(OSError):
        winjob.Job(None)
    kernel = FakeKernel(set_ok=0)
    monkeypatch.setattr(winjob, "_kernel32", lambda: kernel)
    with pytest.raises(OSError):
        winjob.Job(None)
    assert kernel.log[-1] == "close"
    kernel = FakeKernel(assign_ok=0)
    monkeypatch.setattr(winjob, "_kernel32", lambda: kernel)
    with pytest.raises(OSError):
        winjob.Job(None).assign(7)


def test_services_job_is_named_by_the_profile_and_cached(monkeypatch):
    made = []
    monkeypatch.setattr(winjob, "_jobs", {})
    monkeypatch.setattr(winjob, "Job", lambda name=None, **kw: made.append(name) or object())
    monkeypatch.setattr(winjob, "WINDOWS", False)
    assert winjob.services_job(make_profile()) is None and made == []
    monkeypatch.setattr(winjob, "WINDOWS", True)
    profile = make_profile()
    first = winjob.services_job(profile)
    assert winjob.services_job(profile) is first and made == [profile.job_name]
    other = winjob.services_job(replace(profile, job_name="Local\\other"))
    assert other is not first and made == [profile.job_name, "Local\\other"]


def test_the_module_imports_without_windows_apis():
    # ctypes.WinDLL exists only on Windows; importing the module must not reach for it.
    import importlib
    assert importlib.reload(winjob) is winjob
    assert winjob.WINDOWS == (sys.platform == "win32")


# -- fix round 1 ----------------------------------------------------------------

class FakeThreadKernel:
    """kernel32 for _resume_threads: one thread owned by `pid`; ResumeThread returns `resume`
    (with the default c_int restype, Windows' failure value (DWORD)-1 arrives as -1)."""

    def __init__(self, pid, resume):
        self.pid, self.resume, self.closed = pid, resume, []
        kernel = self

        class Fn:
            def __init__(self, impl):
                self.impl = impl

            def __call__(self, *args):
                return self.impl(*args)

        def first(snapshot, pointer):
            pointer._obj.th32OwnerProcessID, pointer._obj.th32ThreadID = kernel.pid, 77
            return 1

        self.CreateToolhelp32Snapshot = Fn(lambda flags, pid: 99)
        self.Thread32First = Fn(first)
        self.Thread32Next = Fn(lambda snapshot, pointer: 0)
        self.OpenThread = Fn(lambda access, inherit, tid: 55)
        self.ResumeThread = Fn(lambda thread: kernel.resume)
        self.CloseHandle = Fn(lambda handle: kernel.closed.append(handle) or 1)


@pytest.mark.parametrize("failure", [-1, 0xFFFFFFFF])
def test_a_failed_resume_thread_is_resume_failed(windows, monkeypatch, failure):
    kernel = FakeThreadKernel(4242, failure)
    monkeypatch.setattr(winjob, "_kernel32", lambda: kernel)
    monkeypatch.setattr(winjob, "_last_error", lambda: OSError("fake"))
    monkeypatch.setattr(winjob, "_resume_threads", RESUME_THREADS)
    with pytest.raises(winjob.SpawnError) as caught:
        winjob.spawn(["x.exe"], FakeJob())
    assert caught.value.result.code == "resume_failed"
    assert FakePopen.killed == [4242]
    assert sorted(kernel.closed) == [55, 99]  # the thread and the snapshot handles


def test_resume_threads_declares_every_kernel_signature(monkeypatch):
    from ctypes import wintypes
    kernel = FakeThreadKernel(4242, 1)
    monkeypatch.setattr(winjob, "_kernel32", lambda: kernel)
    RESUME_THREADS(4242)
    assert kernel.ResumeThread.restype is wintypes.DWORD
    assert kernel.CreateToolhelp32Snapshot.restype is wintypes.HANDLE
    assert kernel.OpenThread.restype is wintypes.HANDLE
    for name in ("CreateToolhelp32Snapshot", "Thread32First", "Thread32Next", "OpenThread",
                 "ResumeThread", "CloseHandle"):
        assert getattr(kernel, name).argtypes, name
    for name in ("Thread32First", "Thread32Next", "CloseHandle"):
        assert getattr(kernel, name).restype is wintypes.BOOL, name


RESUME_THREADS = winjob._resume_threads


def test_services_job_creates_one_job_across_threads(monkeypatch):
    import threading
    import time
    made = []

    def slow_job(name=None, **kw):
        made.append(name)
        time.sleep(0.05)  # widen the check-then-create window
        return object()

    monkeypatch.setattr(winjob, "_jobs", {})
    monkeypatch.setattr(winjob, "Job", slow_job)
    monkeypatch.setattr(winjob, "WINDOWS", True)
    profile, got, gate = make_profile(), [], threading.Barrier(2)

    def call():
        gate.wait()
        got.append(winjob.services_job(profile))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(made) == 1 and got[0] is got[1]


def test_a_failed_assign_reaps_the_child_and_closes_its_pipes(windows):
    with pytest.raises(winjob.SpawnError):
        winjob.spawn(["x.exe"], FakeJob(fail=True))
    assert FakePopen.waited == [(4242, 5)]
    assert all(pipe.closed for pipe in FakePopen.last.pipes)


def test_job_keeps_no_dead_ctypes_attribute(monkeypatch):
    monkeypatch.setattr(winjob, "_kernel32", lambda: FakeKernel())
    assert not hasattr(winjob.Job(None), "_ctypes")
