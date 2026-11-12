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


class FakePopen:
    """Records every creation; raises PermissionError for the flags in `refuse`."""
    calls, killed, refuse = [], [], 0

    def __init__(self, argv, **kwargs):
        flags = kwargs.get("creationflags", 0)
        FakePopen.calls.append((argv, kwargs))
        if FakePopen.refuse and flags & FakePopen.refuse:
            raise PermissionError(5, "Access is denied")
        self.pid, self._handle = 4242, 7

    def kill(self):
        FakePopen.killed.append(self.pid)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(winjob, "WINDOWS", True)
    monkeypatch.setattr(winjob.subprocess, "Popen", FakePopen)
    resumed = []
    monkeypatch.setattr(winjob, "_resume_threads", resumed.append)
    FakePopen.calls, FakePopen.killed, FakePopen.refuse = [], [], 0
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
