"""Control flow of control_transport's Win32 code, against a scripted fake API.

Real pipes are covered by tests/test_control_transport_windows.py, which runs on
Windows only. These drive the same PipeServer / PipeConnection / _win_connect
code on any OS by replacing the ctypes bindings with a fake whose return values
each test scripts, so the branches that are hard to provoke for real (an
instance that cannot be re-created, WAIT_FAILED, an odd WaitNamedPipeW error)
are still exercised.
"""
import ctypes
import ctypes.wintypes as wt
import importlib.util
import socket
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "control_transport.py"


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


class FakeCtypes:
    """ctypes, plus the Windows-only get_last_error / WinError."""

    def __init__(self):
        self.last = 0

    def __getattr__(self, name):
        return getattr(ctypes, name)

    def get_last_error(self):
        return self.last

    def WinError(self, code=None):
        e = OSError(f"[WinError {code}]")
        e.winerror = code
        return e


class FakeAPI:
    def __init__(self):
        self.ctypes = FakeCtypes()
        self.w = wt
        self.OVERLAPPED, self.SECURITY_ATTRIBUTES = OVERLAPPED, SECURITY_ATTRIBUTES
        self.calls = []
        self.next_handle = 100
        self.create_pipe = []      # scripted (handle or None, error) per call
        self.connect_pipe = []     # scripted (ret, error)
        self.wait_single = []      # scripted return codes
        self.create_file = []      # scripted (handle or None, error)
        self.wait_named = []       # scripted (ret, error)
        self.create_event = []     # scripted handle (0 = failure)

    def _h(self):
        self.next_handle += 1
        return self.next_handle

    def _rec(self, name, *args):
        self.calls.append((name,) + args)

    def CreateNamedPipeW(self, name, mode, *rest):
        self._rec("CreateNamedPipeW", mode)
        h, err = self.create_pipe.pop(0) if self.create_pipe else (self._h(), 0)
        self.ctypes.last = err
        return h if h is not None else ctypes.c_void_p(-1).value

    def ConnectNamedPipe(self, h, ov):
        self._rec("ConnectNamedPipe", h)
        ret, err = self.connect_pipe.pop(0) if self.connect_pipe else (0, 997)
        self.ctypes.last = err
        return ret

    def WaitForSingleObject(self, h, ms):
        self._rec("WaitForSingleObject", ms)
        r = self.wait_single.pop(0) if self.wait_single else 0x102
        self.ctypes.last = 6 if r == 0xFFFFFFFF else 0
        return r

    def GetOverlappedResult(self, h, ov, n, wait):
        self._rec("GetOverlappedResult", bool(wait))
        return 1

    def CancelIoEx(self, h, ov):
        self._rec("CancelIoEx", h)
        return 1

    def CreateEventW(self, *a):
        h = self.create_event.pop(0) if self.create_event else self._h()
        self._rec("CreateEventW", h)
        self.ctypes.last = 0 if h else 8
        return h

    def CloseHandle(self, h):
        self._rec("CloseHandle", h)
        return 1

    def LocalFree(self, p):
        return None

    def ConvertStringSecurityDescriptorToSecurityDescriptorW(self, sddl, rev, psd, size):
        self._rec("SDDL", sddl)
        return 1

    def CreateFileW(self, name, *rest):
        self._rec("CreateFileW", name)
        h, err = self.create_file.pop(0) if self.create_file else (self._h(), 0)
        self.ctypes.last = err
        return h if h is not None else ctypes.c_void_p(-1).value

    def WaitNamedPipeW(self, name, ms):
        self._rec("WaitNamedPipeW", ms)
        ret, err = self.wait_named.pop(0) if self.wait_named else (1, 0)
        self.ctypes.last = err
        return ret

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


@pytest.fixture()
def ct(monkeypatch):
    spec = importlib.util.spec_from_file_location("vml_ct_winflow", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    api = FakeAPI()
    mod._API = api
    monkeypatch.setattr(mod, "current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(mod, "current_integrity_rid", lambda: 0x2000)
    mod.fake = api
    return mod


FIRST = 0x00080000


def test_server_carries_a_mandatory_label_no_higher_than_medium(ct):
    ct.PipeServer("\\\\.\\pipe\\x")
    sddl = [c[1] for c in ct.fake.calls if c[0] == "SDDL"][0]
    assert sddl.endswith("S:(ML;;NW;;;S-1-16-8192)")


def test_connect_named_pipe_returning_nonzero_counts_as_connected(ct):
    srv = ct.PipeServer("\\\\.\\pipe\\x")
    ct.fake.connect_pipe = [(1, 0)]
    srv.settimeout(0.1)
    conn, _ = srv.accept()
    assert isinstance(conn, ct.PipeConnection)


def test_rearm_after_a_failed_instance_creation_uses_first_instance(ct):
    srv = ct.PipeServer("\\\\.\\pipe\\x")
    srv.settimeout(0.1)
    ct.fake.connect_pipe = [(0, 535)]                 # client already connected
    ct.fake.create_pipe = [(None, 231)]               # the next instance fails
    conn, _ = srv.accept()
    ct.fake.calls.clear()
    ct.fake.wait_single = [0x102]
    with pytest.raises(socket.timeout):
        srv.accept()
    modes = [c[1] for c in ct.fake.calls if c[0] == "CreateNamedPipeW"]
    assert modes and modes[0] & FIRST


def test_failed_rearm_does_not_spin(ct):
    srv = ct.PipeServer("\\\\.\\pipe\\x")
    srv.settimeout(0.2)
    ct.fake.connect_pipe = [(0, 535)]
    ct.fake.create_pipe = [(None, 231), (None, 5)]
    srv.accept()
    t = time.monotonic()
    with pytest.raises(OSError):
        srv.accept()
    assert time.monotonic() - t >= 0.15


def test_wait_failed_in_accept_raises_without_a_blocking_wait(ct):
    srv = ct.PipeServer("\\\\.\\pipe\\x")
    srv.settimeout(0.1)
    ct.fake.wait_single = [0xFFFFFFFF]
    ct.fake.calls.clear()
    with pytest.raises(OSError):
        srv.accept()
    assert ct.fake.count("CancelIoEx") == 1
    assert ("GetOverlappedResult", True) not in ct.fake.calls
    srv.close()  # stop() must return: no blocking wait here either
    assert ("GetOverlappedResult", True) not in ct.fake.calls


def test_pipe_connection_closes_the_handle_if_init_fails(ct):
    ct.fake.create_event = [0]
    with pytest.raises(OSError):
        ct.PipeConnection(4242)
    assert ("CloseHandle", 4242) in ct.fake.calls


def test_connect_raises_on_unexpected_wait_named_pipe_error(ct):
    ct.fake.create_file = [(None, 231)] * 50          # always busy
    ct.fake.wait_named = [(0, 5)] * 50                # access denied, not a timeout
    with pytest.raises(OSError) as e:
        ct._win_connect("\\\\.\\pipe\\x", 1.0)
    assert not isinstance(e.value, socket.timeout)
    assert ct.fake.count("CreateFileW") == 1


def test_connect_respects_the_timeout_when_busy_forever(ct):
    ct.fake.create_file = [(None, 231)] * 100000
    ct.fake.wait_named = [(1, 0)] * 100000            # "an instance is free" ... but it never is
    t = time.monotonic()
    with pytest.raises(socket.timeout):
        ct._win_connect("\\\\.\\pipe\\x", 0.2)
    assert time.monotonic() - t < 1.0


def test_connect_fails_closed_when_the_server_cannot_be_verified(ct, monkeypatch):
    monkeypatch.setattr(ct, "pipe_owner_sid", lambda conn: "S-1-5-21-1-2-3-1001")

    def boom(conn):
        raise OSError("GetNamedPipeServerProcessId failed")
    monkeypatch.setattr(ct, "server_identity", boom)
    with pytest.raises(PermissionError):
        ct._win_connect("\\\\.\\pipe\\x", 1.0)
    handle = [c[1] for c in ct.fake.calls if c[0] == "CloseHandle"]
    assert handle, "the connection must be closed"


@pytest.mark.parametrize("server, trusted", [
    (("S-1-5-21-1-2-3-1001", 0x2000), True),    # same user, same integrity
    (("S-1-5-21-1-2-3-1001", 0x3000), True),    # same user, elevated
    (("S-1-5-21-1-2-3-1001", 0x1000), False),   # same user, Low: the squatter
    (("S-1-5-21-1-2-3-1002", 0x2000), False),   # another user
])
def test_connect_checks_server_user_and_integrity(ct, monkeypatch, server, trusted):
    monkeypatch.setattr(ct, "pipe_owner_sid", lambda conn: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(ct, "server_identity", lambda conn: server)
    if trusted:
        assert isinstance(ct._win_connect("\\\\.\\pipe\\x", 1.0), ct.PipeConnection)
    else:
        with pytest.raises(PermissionError):
            ct._win_connect("\\\\.\\pipe\\x", 1.0)


def test_unknown_server_integrity_is_refused_with_a_readable_error(ct, monkeypatch):
    monkeypatch.setattr(ct, "pipe_owner_sid", lambda conn: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(ct, "server_identity", lambda conn: ("S-1-5-21-1-2-3-1001", None))
    with pytest.raises(PermissionError, match="unknown"):
        ct._win_connect("\\\\.\\pipe\\x", 1.0)
