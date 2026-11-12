"""Windows Job Objects, and a spawn that puts a child in one before it runs and records whether it
left the parent's job.

Moved from voicemode_native.py (`Job`, `_resume_threads`, `spawn`, `services_job`). One change in
behaviour: where the parent's job forbids breakaway, the old spawn retried without it and said
nothing; this one still retries, and returns Result("breakaway_refused") so the caller knows the
child is also in the parent's job and dies with it.

ctypes is touched only inside the functions that run on Windows, so the module imports anywhere.
"""
from __future__ import annotations

import contextlib
import subprocess
import sys
import threading

from .result import Result

WINDOWS = sys.platform == "win32"

CREATE_SUSPENDED = 0x4
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x1000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class SpawnError(Exception):
    """A child could not be started inside its job. It was killed before running an instruction;
    `result` says why (job_assign_failed, resume_failed)."""

    def __init__(self, result: Result):
        super().__init__(f"{result.code}: {result.detail}")
        self.result = result


def _kernel32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _last_error() -> OSError:
    import ctypes
    return ctypes.WinError(ctypes.get_last_error())


def _limit_structure():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return Extended


class Job:
    """A Windows Job Object that ends every process in it when its last handle closes.

    Named, it is shared: every process that opens the name holds a handle, and the processes in
    it outlive all but the last of them. With silent_breakaway only the processes assigned to it
    are in it, not what they start: for a parent whose child starts processes that must live in
    jobs of their own (a process already in a job can only join one nested inside it). With
    breakaway, a child leaves the job only when it is created with CREATE_BREAKAWAY_FROM_JOB, as
    spawn() asks for.
    """

    def __init__(self, name: str | None = None, breakaway: bool = False, silent_breakaway: bool = False):
        import ctypes
        from ctypes import wintypes

        k = self._kernel = _kernel32()
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)

        self.handle = k.CreateJobObjectW(None, name)
        if not self.handle:
            raise _last_error()
        info = _limit_structure()()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | (JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_BREAKAWAY_OK if silent_breakaway else 0)
            | (JOB_OBJECT_LIMIT_BREAKAWAY_OK if breakaway else 0))
        if not k.SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                         ctypes.byref(info), ctypes.sizeof(info)):
            error = _last_error()
            self.close()
            raise error

    def assign(self, process_handle) -> None:
        if not self._kernel.AssignProcessToJobObject(self.handle, int(process_handle)):
            raise _last_error()

    def close(self) -> None:
        if self.handle:
            self._kernel.CloseHandle(self.handle)
            self.handle = None


def _resume_threads(pid) -> None:
    """Resume a process created suspended: every thread it owns (it has one)."""
    import ctypes
    from ctypes import wintypes

    k = _kernel32()
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k.Thread32First.restype = wintypes.BOOL
    k.Thread32First.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
    k.Thread32Next.restype = wintypes.BOOL
    k.Thread32Next.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
    k.OpenThread.restype = wintypes.HANDLE
    k.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    # DWORD, not the default c_int: the failure value is (DWORD)-1, which a c_int reads as -1.
    k.ResumeThread.restype = wintypes.DWORD
    k.ResumeThread.argtypes = (wintypes.HANDLE,)
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = (wintypes.HANDLE,)

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

    snapshot = k.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise _last_error()
    resumed = 0
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = k.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = k.OpenThread(0x2, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if thread:
                    if (k.ResumeThread(thread) & 0xFFFFFFFF) != 0xFFFFFFFF:
                        resumed += 1
                    k.CloseHandle(thread)
            more = k.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    if not resumed:
        raise RuntimeError(f"could not resume process {pid}")


def spawn(argv, job: Job | None, **popen_kwargs) -> tuple[subprocess.Popen, Result]:
    """Popen; on Windows with a job, created suspended and put in the job before its first
    instruction, so nothing it starts can escape the job.

    It is first asked to leave the job this process is in (a process in an unrelated job cannot
    join ours): Result("breakaway"). Where that job forbids it, it is created again without the
    request and stays in that job too, so it also dies with it: Result("breakaway_refused").
    Off Windows, or without a job, a plain Popen: Result("no_job").

    Raises SpawnError when the child cannot be put in the job or resumed; it has then been
    killed, having run no instruction outside the job."""
    if not (WINDOWS and job is not None):
        return subprocess.Popen(argv, **popen_kwargs), Result("no_job")
    flags = popen_kwargs.pop("creationflags", 0) | CREATE_SUSPENDED
    try:
        process = subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **popen_kwargs)
        result = Result("breakaway")
    except PermissionError as refused:
        process = subprocess.Popen(argv, creationflags=flags, **popen_kwargs)
        result = Result("breakaway_refused",
                        f"the parent's job does not allow breakaway ({refused}); the child is also in "
                        "the parent's job and ends when that job closes")
    try:
        try:
            job.assign(process._handle)
        except OSError as error:
            raise SpawnError(Result("job_assign_failed",
                                    f"could not put process {process.pid} in its Job Object ({error}); "
                                    "it was killed before it ran")) from error
        try:
            _resume_threads(process.pid)
        except (OSError, RuntimeError) as error:
            raise SpawnError(Result("resume_failed", f"{error}; process {process.pid} was killed")) from error
    except BaseException:
        _discard(process)
        raise
    return process, result


def _discard(process) -> None:
    """Kill a child that must not run, reap it, and close its pipes."""
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=5)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def services_job(profile) -> Job | None:
    """The profile's shared Job Object for its services (named profile.job_name), held open for
    this process's lifetime. None off Windows."""
    if not WINDOWS:
        return None
    with _jobs_lock:
        if profile.job_name not in _jobs:
            _jobs[profile.job_name] = Job(profile.job_name)
        return _jobs[profile.job_name]
