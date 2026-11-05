"""Windows named-pipe transport for voice-mode's control channel (stdlib only).

Windows Python has no ``socket.AF_UNIX``, so upstream's control channel
(``control_socket.py``) has no route there: Stop, Interrupt, Pause, push-to-talk
and the pill's speaking status all go dark. This module gives it one, a named
pipe that offers the handful of socket methods the listener and the clients use
(``settimeout``, ``accept``, ``recv``, ``sendall``, ``close``, ``shutdown``),
so the wire protocol -- newline-delimited JSON -- is unchanged.

Security
========
A pipe has no peer credentials to check after the fact, so access is decided
when the pipe is opened, by its DACL:

* the server creates every instance through ``CreateNamedPipeW`` with an
  explicit security descriptor ``O:<me>D:P(A;;GA;;;<me>)`` -- owned by, and
  open only to, the current user (the stdlib's own pipes pass NULL security,
  which grants the default DACL instead);
* ``FILE_FLAG_FIRST_PIPE_INSTANCE`` on the first instance, so a second server
  -- or a squatter that got there first -- makes the bind fail instead of
  silently sharing the name;
* ``PIPE_REJECT_REMOTE_CLIENTS``, so the pipe is local only;
* a mandatory label ``S:(ML;;NW;;;<min(own, Medium)>)``, so a Low-integrity
  process of the same user cannot open the server's pipe for writing;
* the client opens with ``SECURITY_IDENTIFICATION`` (the server cannot
  impersonate it) and refuses to send anything unless the pipe's owner SID is
  its own AND the process serving the pipe (``GetNamedPipeServerProcessId``)
  runs as the same user at an integrity level at least the client's own. The
  owner check alone is not enough: a Low-integrity process of the same user
  (a sandboxed browser tab, say) can create the pipe while no turn holds it,
  owned by that same SID -- shown on Windows 2026-11-05, when such a squatter
  received a command. Anything that cannot be verified fails closed.

Accepted risk: the name is predictable and free between turns. If another user
(or a Low-integrity process) creates it first, that denies the channel to this
one: our bind fails (``FILE_FLAG_FIRST_PIPE_INSTANCE``) and the channel stays
off for that turn. It cannot receive our commands; it can only
deny the channel, which is the same outcome as the channel being disabled.

Naming
======
``\\\\.\\pipe\\voicemode-control-<first 16 hex of sha256(path)>`` where path is
the normalised absolute ``control.sock`` path, so each install's base dir gets
its own pipe. The name is derived on every call and never cached: FELIX moves
``control.sock`` whenever it rebuilds a runtime.

Elevation
=========
The label is capped at Medium, so an elevated (administrator) server's pipe can
still be *opened* by a Medium client. The client then refuses it anyway: it
cannot open an elevated process's token to check who serves the pipe, and an
unverifiable server is refused. That refusal is logged once per reason, naming
elevation as the likely cause. Run the voice server and its clients at the same
level. (Inferred from Windows' token access rules; not yet run elevated.)

Process identity
================
``process_start_time`` and ``process_alive`` answer "is this pid still the
process that registered?" for the pill, through ``OpenProcess`` and
``GetProcessTimes`` -- never ``os.kill(pid, 0)``, which on Windows terminates.

On POSIX nothing here is used; importing the module loads no Windows API.
Installed into voice_mode/control_transport.py by patches/apply.sh.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import sys
import threading
import time

PIPE_PREFIX = "\\\\.\\pipe\\voicemode-control-"

_API = None  # Windows API bindings, loaded on first use
_ORPHANED = []  # OVERLAPPEDs whose cancelled I/O was not waited for; never freed
_REFUSALS_LOGGED = set()  # refusal reasons already logged by this process

logger = logging.getLogger("voicemode.control_transport")


def use_pipe() -> bool:
    """True when this Python has no AF_UNIX, so the control channel needs a pipe."""
    return not hasattr(socket, "AF_UNIX")


def pipe_name(path, pathmod=os.path) -> str:
    """The pipe name for a control.sock path. Pure: same path, same name.

    ``pathmod`` is ``os.path`` in real use; tests pass ``ntpath`` to compute
    Windows vectors on Linux. Windows paths are case-folded and slash-normalised
    first (``normcase``), so ``C:\\Users\\U`` and ``c:/users/u`` share a pipe.
    """
    p = pathmod.normcase(pathmod.abspath(pathmod.expanduser(os.fspath(path))))
    return PIPE_PREFIX + hashlib.sha256(p.encode("utf-8")).hexdigest()[:16]


_LOW_RID, _MEDIUM_RID, _HIGH_RID, _SYSTEM_RID = 0x1000, 0x2000, 0x3000, 0x4000
_LABEL_ALIASES = {"LW": _LOW_RID, "ME": _MEDIUM_RID, "MP": 0x2100, "HI": _HIGH_RID,
                  "SI": _SYSTEM_RID}


def owner_only_sddl(sid: str, label_rid=None) -> str:
    """Owner and sole grantee is ``sid``; ``P`` blocks inherited ACEs. With
    ``label_rid``, a no-write-up mandatory label at that integrity level."""
    sddl = f"O:{sid}D:P(A;;GA;;;{sid})"
    if label_rid is not None:
        sddl += f"S:(ML;;NW;;;S-1-16-{label_rid})"
    return sddl


def server_label_rid(own_rid: int) -> int:
    """The server's label: its own level, capped at Medium; never above its own,
    which Windows would refuse anyway. The cap only keeps the label from blocking
    a Medium client's open of an elevated server's pipe. It does not make that
    pairing work: the client's own check (``_verify_server``) then refuses the
    server, because a Medium process cannot read an elevated process's token."""
    return min(own_rid, _MEDIUM_RID)


def sddl_label_rid(sddl: str):
    """Integrity RID of the mandatory label in an SDDL string, or None."""
    m = re.search(r"\(ML;[^;]*;[^;]*;[^;]*;[^;]*;(?P<sid>[^)]+)\)", sddl)
    if not m:
        return None
    sid = m.group("sid")
    if sid in _LABEL_ALIASES:
        return _LABEL_ALIASES[sid]
    if sid.startswith("S-1-16-"):
        return int(sid[len("S-1-16-"):])
    return None


def server_is_trusted(server_sid, server_rid, my_sid, my_rid) -> bool:
    """Trust a pipe server only if it runs as us, at our integrity or above.
    Unknown values fail closed."""
    if not server_sid or server_rid is None or my_rid is None:
        return False
    return server_sid == my_sid and server_rid >= my_rid


def sddl_is_owner_only(sddl: str, sid: str) -> bool:
    """True if a read-back descriptor is owned by ``sid``, protected, and has
    exactly one ACE: an allow for ``sid``."""
    m = re.fullmatch(r"O:(?P<o>[^:]+?)(?:G:[^:]+?)?D:(?P<flags>[A-Z]*)(?P<aces>(?:\([^)]*\))*)"
                     r"(?:S:[A-Z]*(?:\([^)]*\))*)?", sddl)
    if not m or m.group("o") != sid or "P" not in m.group("flags"):
        return False
    aces = re.findall(r"\(([^)]*)\)", m.group("aces"))
    if len(aces) != 1:
        return False
    parts = aces[0].split(";")
    return len(parts) == 6 and parts[0] == "A" and parts[5] == sid


def create_server(path):
    """Create the server end for ``path`` and return it (Windows only).

    Raises ``PermissionError`` if the name is already taken (another server, or
    anyone else), and ``OSError`` off Windows or on any other failure.
    """
    return _win_create_server(pipe_name(path))


def connect(path, timeout):
    """Open the pipe for ``path`` and return a connection (Windows only).

    Raises ``FileNotFoundError`` when no server is listening, ``PermissionError``
    when the pipe's owner is not the current user (nothing is sent), and
    ``socket.timeout`` when every instance stays busy past ``timeout``.
    """
    return _win_connect(pipe_name(path), timeout)


# --- Windows implementation -------------------------------------------------

def _api():
    global _API
    if _API is not None:
        return _API
    if sys.platform != "win32":
        raise OSError("named-pipe control transport is Windows-only")
    import ctypes
    from ctypes import wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", w.BOOL)]

    def sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)
        return fn

    P = ctypes.c_void_p
    LP = ctypes.POINTER
    api = type("WinAPI", (), {})()
    api.ctypes, api.w, api.OVERLAPPED, api.SECURITY_ATTRIBUTES = ctypes, w, OVERLAPPED, SECURITY_ATTRIBUTES
    api.CreateNamedPipeW = sig(k32.CreateNamedPipeW, w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
                               w.DWORD, w.DWORD, w.DWORD, w.DWORD, LP(SECURITY_ATTRIBUTES))
    api.ConnectNamedPipe = sig(k32.ConnectNamedPipe, w.BOOL, w.HANDLE, LP(OVERLAPPED))
    api.CreateFileW = sig(k32.CreateFileW, w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, P,
                          w.DWORD, w.DWORD, w.HANDLE)
    api.WaitNamedPipeW = sig(k32.WaitNamedPipeW, w.BOOL, w.LPCWSTR, w.DWORD)
    api.ReadFile = sig(k32.ReadFile, w.BOOL, w.HANDLE, P, w.DWORD, LP(w.DWORD), LP(OVERLAPPED))
    api.WriteFile = sig(k32.WriteFile, w.BOOL, w.HANDLE, P, w.DWORD, LP(w.DWORD), LP(OVERLAPPED))
    api.GetOverlappedResult = sig(k32.GetOverlappedResult, w.BOOL, w.HANDLE, LP(OVERLAPPED),
                                  LP(w.DWORD), w.BOOL)
    api.CancelIoEx = sig(k32.CancelIoEx, w.BOOL, w.HANDLE, LP(OVERLAPPED))
    api.CreateEventW = sig(k32.CreateEventW, w.HANDLE, P, w.BOOL, w.BOOL, w.LPCWSTR)
    api.WaitForSingleObject = sig(k32.WaitForSingleObject, w.DWORD, w.HANDLE, w.DWORD)
    api.CloseHandle = sig(k32.CloseHandle, w.BOOL, w.HANDLE)
    api.LocalFree = sig(k32.LocalFree, P, P)
    api.GetCurrentProcess = sig(k32.GetCurrentProcess, w.HANDLE)
    api.OpenProcessToken = sig(adv.OpenProcessToken, w.BOOL, w.HANDLE, w.DWORD, LP(w.HANDLE))
    api.GetTokenInformation = sig(adv.GetTokenInformation, w.BOOL, w.HANDLE, ctypes.c_int, P,
                                  w.DWORD, LP(w.DWORD))
    api.ConvertSidToStringSidW = sig(adv.ConvertSidToStringSidW, w.BOOL, P, LP(w.LPWSTR))
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW = sig(
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW, w.BOOL, w.LPCWSTR, w.DWORD,
        LP(P), LP(w.ULONG))
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW = sig(
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW, w.BOOL, P, w.DWORD, w.DWORD,
        LP(w.LPWSTR), LP(w.ULONG))
    api.GetSecurityInfo = sig(adv.GetSecurityInfo, w.DWORD, w.HANDLE, ctypes.c_int, w.DWORD,
                              LP(P), LP(P), LP(P), LP(P), LP(P))
    api.GetNamedPipeServerProcessId = sig(k32.GetNamedPipeServerProcessId, w.BOOL, w.HANDLE,
                                          LP(w.ULONG))
    api.OpenProcess = sig(k32.OpenProcess, w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    api.GetSidSubAuthorityCount = sig(adv.GetSidSubAuthorityCount, LP(ctypes.c_ubyte), P)
    api.GetSidSubAuthority = sig(adv.GetSidSubAuthority, LP(w.DWORD), P, w.DWORD)

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", w.DWORD), ("dwHighDateTime", w.DWORD)]

    api.FILETIME = FILETIME
    api.GetProcessTimes = sig(k32.GetProcessTimes, w.BOOL, w.HANDLE, LP(FILETIME), LP(FILETIME),
                              LP(FILETIME), LP(FILETIME))
    api.GetExitCodeProcess = sig(k32.GetExitCodeProcess, w.BOOL, w.HANDLE, LP(w.DWORD))
    _API = api
    return api


# Win32 constants
_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_FILE_FLAG_OVERLAPPED = 0x40000000
_PIPE_TYPE_BYTE = 0x0
_PIPE_READMODE_BYTE = 0x0
_PIPE_WAIT = 0x0
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_UNLIMITED_INSTANCES = 255
_BUFSIZE = 65536
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000
_INVALID_HANDLE_VALUE = -1
_INFINITE = 0xFFFFFFFF
_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102
_ERROR_FILE_NOT_FOUND = 2
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_BUSY = 231
_ERROR_NO_DATA = 232
_ERROR_PIPE_NOT_CONNECTED = 233
_ERROR_PIPE_CONNECTED = 535
_ERROR_OPERATION_ABORTED = 995
_ERROR_IO_PENDING = 997
_ERROR_SEM_TIMEOUT = 121
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_INTEGRITY_LEVEL = 25
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_LABEL_SECURITY_INFORMATION = 0x10
_WAIT_FAILED = 0xFFFFFFFF
_SE_KERNEL_OBJECT = 6
_OWNER_SECURITY_INFORMATION = 0x1
_DACL_SECURITY_INFORMATION = 0x4
_SDDL_REVISION_1 = 1
_EOF_ERRORS = (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED, _ERROR_NO_DATA)


def _winerr(api, err=None):
    err = api.ctypes.get_last_error() if err is None else err
    return api.ctypes.WinError(err)


def _ms(timeout) -> int:
    if timeout is None:
        return _INFINITE
    if timeout <= 0:
        return 0
    return max(1, min(int(timeout * 1000), _INFINITE - 1))


def _handle_ok(h) -> bool:
    # HANDLE is c_void_p, so INVALID_HANDLE_VALUE comes back as 2**64-1, not -1.
    # Missing this let a failed CreateFileW through, and the owner check then
    # read the current *process* (pseudo-handle -1) and passed. Found on Windows.
    if h is None or h == 0:
        return False
    import ctypes
    return h != _INVALID_HANDLE_VALUE and h != ctypes.c_void_p(_INVALID_HANDLE_VALUE).value


def _sid_string(api, psid) -> str:
    out = api.w.LPWSTR()
    if not api.ConvertSidToStringSidW(psid, api.ctypes.byref(out)):
        raise _winerr(api)
    try:
        return out.value
    finally:
        api.LocalFree(api.ctypes.cast(out, api.ctypes.c_void_p))


def _token_sid_buffer(api, tok, info_class):
    """The SID inside a TOKEN_USER / TOKEN_MANDATORY_LABEL (both start with a
    SID_AND_ATTRIBUTES), plus the buffer that owns it."""
    ct = api.ctypes
    size = api.w.DWORD()
    api.GetTokenInformation(tok, info_class, None, 0, ct.byref(size))
    if not size.value:
        raise _winerr(api)
    buf = ct.create_string_buffer(size.value)
    if not api.GetTokenInformation(tok, info_class, buf, size, ct.byref(size)):
        raise _winerr(api)
    return ct.cast(buf, ct.POINTER(ct.c_void_p))[0], buf


def _process_identity(api, process):
    """(user SID string, integrity RID) of a process handle's primary token."""
    ct = api.ctypes
    tok = api.w.HANDLE()
    if not api.OpenProcessToken(process, _TOKEN_QUERY, ct.byref(tok)):
        raise _winerr(api)
    try:
        user, _keep = _token_sid_buffer(api, tok, _TOKEN_USER)
        sid = _sid_string(api, user)
        label, _keep2 = _token_sid_buffer(api, tok, _TOKEN_INTEGRITY_LEVEL)
        count = api.GetSidSubAuthorityCount(label)[0]
        if count < 1:
            raise OSError("integrity label SID has no sub-authority")
        rid = api.GetSidSubAuthority(label, count - 1)[0]
        return sid, int(rid)
    finally:
        api.CloseHandle(tok)


def current_user_sid() -> str:
    """String SID of the user this process runs as (the token's user)."""
    api = _api()
    return _process_identity(api, api.GetCurrentProcess())[0]


def current_integrity_rid() -> int:
    """This process's integrity level RID (0x1000 Low, 0x2000 Medium, ...)."""
    api = _api()
    return _process_identity(api, api.GetCurrentProcess())[1]


def filetime_token(high: int, low: int) -> str:
    """A process creation FILETIME as an opaque, comparable text token."""
    return str((int(high) << 32) | int(low))


def _open_for_query(api, pid):
    """(handle, can_wait) for a pid, or (None, last error). SYNCHRONIZE first, so a
    wait can tell an exited process from one with exit code 259; without it,
    fall back to the exit code."""
    h = api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, int(pid))
    if _handle_ok(h):
        return h, True
    h = api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if _handle_ok(h):
        return h, False
    return None, api.ctypes.get_last_error()


def _still_running(api, h, can_wait) -> bool:
    # An exited process lives on while anyone holds a handle to it.
    if can_wait:
        return api.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
    code = api.w.DWORD()
    return bool(api.GetExitCodeProcess(h, api.ctypes.byref(code))) and code.value == _STILL_ACTIVE


def process_start_time(pid: int):
    """Creation-time token of a running pid, or None if it is gone or cannot be queried
    (Windows only; raises OSError elsewhere)."""
    api = _api()
    if int(pid) <= 0:
        return None
    h, can_wait = _open_for_query(api, pid)
    if h is None:
        return None
    try:
        if not _still_running(api, h, can_wait):
            return None
        ft = [api.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(h, *(api.ctypes.byref(f) for f in ft)):
            return None
        return filetime_token(ft[0].dwHighDateTime, ft[0].dwLowDateTime)
    finally:
        api.CloseHandle(h)


def process_alive(pid: int) -> bool:
    """True if pid is a running process, including one we may not open (access
    denied means it exists). Windows only; raises OSError elsewhere."""
    api = _api()
    if int(pid) <= 0:
        return False
    h, can_wait = _open_for_query(api, pid)
    if h is None:
        return can_wait == _ERROR_ACCESS_DENIED
    try:
        return _still_running(api, h, can_wait)
    finally:
        api.CloseHandle(h)


def server_identity(conn):
    """(user SID, integrity RID) of the process serving a connected pipe."""
    api = _api()
    ct = api.ctypes
    pid = api.w.ULONG()
    if not api.GetNamedPipeServerProcessId(conn._handle, ct.byref(pid)):
        raise _winerr(api)
    proc = api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not _handle_ok(proc):
        raise _winerr(api)
    try:
        return _process_identity(api, proc)
    finally:
        api.CloseHandle(proc)


def _security_info(api, handle, what):
    """Return (owner SID string or None, SDDL string) for a handle."""
    ct = api.ctypes
    owner, sd = ct.c_void_p(), ct.c_void_p()
    err = api.GetSecurityInfo(handle, _SE_KERNEL_OBJECT, what, ct.byref(owner), None, None,
                              None, ct.byref(sd))
    if err:
        raise _winerr(api, err)
    try:
        sddl = api.w.LPWSTR()
        if not api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                sd, _SDDL_REVISION_1, what, ct.byref(sddl), None):
            raise _winerr(api)
        try:
            text = sddl.value
        finally:
            api.LocalFree(ct.cast(sddl, ct.c_void_p))
        return (_sid_string(api, owner) if owner.value else None), text
    finally:
        api.LocalFree(sd)


def pipe_owner_sid(conn) -> str:
    """Owner SID of the pipe behind a connection (or server)."""
    api = _api()
    owner, _ = _security_info(api, conn._handle, _OWNER_SECURITY_INFORMATION)
    return owner


def pipe_security_sddl(conn) -> str:
    """Owner, DACL and mandatory label of the pipe, read back from the handle."""
    api = _api()
    return _security_info(api, conn._handle,
                          _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION
                          | _LABEL_SECURITY_INFORMATION)[1]


def _overlapped(api):
    ev = api.CreateEventW(None, True, False, None)
    if not _handle_ok(ev):
        raise _winerr(api)
    ov = api.OVERLAPPED()
    ov.hEvent = ev
    return ov


def _complete(api, handle, ov, ok, timeout):
    """Finish an overlapped call. Returns (error, bytes); error -1 means timed out.

    On timeout the I/O is cancelled and waited for, so the buffer and OVERLAPPED
    are never written after we return; a completion that beat the cancel wins.
    """
    ct = api.ctypes
    timed_out = False
    if not ok:
        err = ct.get_last_error()
        if err != _ERROR_IO_PENDING:
            return err, 0
        r = api.WaitForSingleObject(ov.hEvent, _ms(timeout))
        if r == _WAIT_TIMEOUT:
            api.CancelIoEx(handle, ct.byref(ov))
            timed_out = True
        elif r != _WAIT_OBJECT_0:
            err = ct.get_last_error()
            api.CancelIoEx(handle, ct.byref(ov))
            api.GetOverlappedResult(handle, ct.byref(ov), ct.byref(api.w.DWORD()), True)
            return err or 6, 0
    n = api.w.DWORD()
    if api.GetOverlappedResult(handle, ct.byref(ov), ct.byref(n), True):
        return 0, n.value
    err = ct.get_last_error()
    if timed_out and err == _ERROR_OPERATION_ABORTED:
        return -1, 0
    return err, n.value


class PipeConnection:
    """One connected pipe end, with the socket methods the control code uses."""

    def __init__(self, handle):
        self._closed = True  # until fully built, so __del__/close do nothing
        self._api = api = _api()
        self._handle = handle
        try:
            self._ov = _overlapped(api)
        except BaseException:
            api.CloseHandle(handle)  # never leak the pipe end we were given
            raise
        self._timeout = None
        self._closed = False

    def settimeout(self, timeout):
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def _check(self):
        if self._closed:
            raise OSError(9, "pipe connection is closed")

    def recv(self, bufsize: int) -> bytes:
        """Up to ``bufsize`` bytes; ``b""`` once the other end has closed."""
        self._check()
        api = self._api
        ct = api.ctypes
        buf = ct.create_string_buffer(bufsize)
        ok = api.ReadFile(self._handle, buf, bufsize, None, ct.byref(self._ov))
        err, n = _complete(api, self._handle, self._ov, ok, self._timeout)
        if err == -1:
            raise socket.timeout("timed out")
        if err in _EOF_ERRORS:
            return b""
        if err:
            raise _winerr(api, err)
        return buf.raw[:n]

    def sendall(self, data: bytes) -> None:
        self._check()
        api = self._api
        ct = api.ctypes
        view = memoryview(bytes(data))
        while view:
            chunk = ct.create_string_buffer(view.tobytes(), len(view))
            ok = api.WriteFile(self._handle, chunk, len(view), None, ct.byref(self._ov))
            err, n = _complete(api, self._handle, self._ov, ok, self._timeout)
            if err == -1:
                raise socket.timeout("timed out")
            if err in _EOF_ERRORS:
                raise BrokenPipeError(32, "control pipe closed by peer")
            if err:
                raise _winerr(api, err)
            view = view[n:]

    def shutdown(self, how=None) -> None:
        """A pipe cannot be half-closed; accepted for socket compatibility."""

    def close(self) -> None:
        # CloseHandle, not DisconnectNamedPipe: a disconnect would discard a
        # status reply the client has not read yet.
        if self._closed:
            return
        self._closed = True
        self._api.CloseHandle(self._handle)
        self._api.CloseHandle(self._ov.hEvent)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter teardown
            pass


class PipeServer:
    """The listening end. Always holds one unconnected instance, so the name
    stays ours between connections; ``accept`` hands that instance to the
    caller and creates the next one before returning."""

    def __init__(self, name: str):
        self._api = api = _api()
        self.name = name
        self._timeout = None
        self._lock = threading.Lock()
        self._closed = False
        self._in_accept = False
        self._pending = False
        self._cleaned = True  # nothing to release until fully built
        self._broken = False
        self._handle = None
        self._sid = current_user_sid()
        sd = api.ctypes.c_void_p()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                owner_only_sddl(self._sid, server_label_rid(current_integrity_rid())),
                _SDDL_REVISION_1, api.ctypes.byref(sd), None):
            raise _winerr(api)
        self._sd = sd
        self._sa = api.SECURITY_ATTRIBUTES(api.ctypes.sizeof(api.SECURITY_ATTRIBUTES), sd, False)
        try:
            self._handle = self._new_instance(first=True)
            self._ov = _overlapped(api)
        except BaseException:
            if self._handle is not None:
                api.CloseHandle(self._handle)
                self._handle = None
            api.LocalFree(self._sd)
            raise
        self._cleaned = False

    def _new_instance(self, first: bool):
        api = self._api
        mode = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
        if first:
            mode |= _FILE_FLAG_FIRST_PIPE_INSTANCE
        h = api.CreateNamedPipeW(
            self.name, mode,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
            _PIPE_UNLIMITED_INSTANCES, _BUFSIZE, _BUFSIZE, 0, api.ctypes.byref(self._sa))
        if not _handle_ok(h):
            raise _winerr(api)
        return h

    def settimeout(self, timeout):
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def listen(self, backlog=None) -> None:
        """Instances are created on demand; accepted for socket compatibility."""

    def accept(self):
        """Wait up to the timeout for a client. Returns ``(PipeConnection, None)``.

        Raises ``socket.timeout`` when nobody connects in time, and ``OSError``
        once the server is closed (also from another thread, mid-wait)."""
        api = self._api
        ct = api.ctypes
        with self._lock:
            if self._closed:
                raise OSError(9, "pipe server is closed")
            self._in_accept = True
        try:
            if self._broken:
                self._pause()
                raise OSError(6, "pipe server is broken (wait failed); recreate it")
            if self._handle is None:
                # A previous hand-over could not re-arm. Re-create as the FIRST
                # instance: if anyone took the name meanwhile, fail rather than
                # join them. Pause first so a persistent failure cannot spin
                # the caller's accept loop.
                try:
                    self._handle = self._new_instance(first=True)
                except OSError:
                    self._pause()
                    raise
            if not self._pending:
                ok = api.ConnectNamedPipe(self._handle, ct.byref(self._ov))
                err = 0 if ok else ct.get_last_error()
                # Nonzero: connected. PIPE_CONNECTED: connected before we
                # asked. NO_DATA: connected, wrote and closed already. In every
                # case hand it over, so a command still in the buffer is read.
                if ok or err in (_ERROR_PIPE_CONNECTED, _ERROR_NO_DATA):
                    return self._hand_over()
                if err != _ERROR_IO_PENDING:
                    raise _winerr(api, err or 6)
                self._pending = True
            r = api.WaitForSingleObject(self._ov.hEvent, _ms(self._timeout))
            if r not in (_WAIT_OBJECT_0, _WAIT_TIMEOUT):
                # WAIT_FAILED: the event is unusable, so a blocking
                # GetOverlappedResult could hang stop(). Cancel and give up on
                # this server; the OVERLAPPED is kept alive for the cancel.
                err = ct.get_last_error()
                api.CancelIoEx(self._handle, ct.byref(self._ov))
                self._broken = True
                _ORPHANED.append(self._ov)
                raise _winerr(api, err or 6)
            if r == _WAIT_TIMEOUT:
                if self._closed:
                    raise OSError(9, "pipe server is closed")
                raise socket.timeout("timed out")
            n = api.w.DWORD()
            ok = api.GetOverlappedResult(self._handle, ct.byref(self._ov), ct.byref(n), True)
            self._pending = False
            if not ok:
                raise _winerr(api)
            if self._closed:
                raise OSError(9, "pipe server is closed")
            return self._hand_over()
        finally:
            with self._lock:
                self._in_accept = False
                if self._closed:
                    self._cleanup_locked()

    def _pause(self):
        time.sleep(0.25 if self._timeout is None else max(0.0, min(self._timeout, 0.25)))

    def _hand_over(self):
        self._pending = False
        conn_handle, self._handle = self._handle, None
        # The connected handle stays open while the next instance is created,
        # so there is never a moment when the name is free for someone else.
        # If that fails, the client is still served and the next accept retries.
        try:
            self._handle = self._new_instance(first=False)
        except OSError:
            pass
        return PipeConnection(conn_handle), None

    def shutdown(self, how=None) -> None:
        """Accepted for socket compatibility; use ``close``."""

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._in_accept:
                # The accept thread is waiting on our event: wake it and let it
                # release the handles, rather than closing them under it.
                if self._pending and self._handle is not None:
                    self._api.CancelIoEx(self._handle, self._api.ctypes.byref(self._ov))
                return
            self._cleanup_locked()

    def _cleanup_locked(self):
        if self._cleaned:
            return
        self._cleaned = True
        api = self._api
        h, self._handle = self._handle, None
        if h is not None:
            if self._pending:
                api.CancelIoEx(h, api.ctypes.byref(self._ov))
                if not self._broken:  # a broken wait must not block close()
                    api.GetOverlappedResult(h, api.ctypes.byref(self._ov),
                                            api.ctypes.byref(api.w.DWORD()), True)
                self._pending = False
            api.CloseHandle(h)
        api.CloseHandle(self._ov.hEvent)
        api.LocalFree(self._sd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        # A dropped server must not hold the name (FIRST_PIPE_INSTANCE) forever.
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter teardown / half-built
            pass


def _win_create_server(name: str) -> PipeServer:
    return PipeServer(name)


def _verify_server(conn, name) -> None:
    """Raise PermissionError unless the pipe is ours and served by us.

    Owner SID first (cheap), then the serving process: same user and an
    integrity level at least our own, so a Low-integrity squatter of the same
    user is refused. Any failure to find out is a refusal (fail closed)."""
    try:
        me = current_user_sid()
        owner = pipe_owner_sid(conn)
    except Exception as exc:  # noqa: BLE001 - cannot verify: refuse
        raise PermissionError(5, f"cannot verify the control pipe: {exc}", name) from exc
    if owner != me:
        raise PermissionError(5, f"control pipe owner {owner} is not the current user {me}", name)
    try:
        server_sid, server_rid = server_identity(conn)
        my_rid = current_integrity_rid()
    except Exception as exc:  # noqa: BLE001 - cannot verify: refuse
        if getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED:
            reason = ("the process serving the control pipe cannot be inspected (access denied); "
                      "most likely the voice server runs elevated (as administrator) and this "
                      "client does not. Run both elevated, or neither")
            _log_refusal_once(reason)
            raise PermissionError(5, f"control pipe refused: {reason}", name) from exc
        raise PermissionError(5, f"cannot verify the control pipe's server: {exc}", name) from exc
    if not server_is_trusted(server_sid, server_rid, me, my_rid):
        raise PermissionError(
            5, f"control pipe is served by {server_sid} at integrity {_hex(server_rid)}; "
               f"need {me} at {_hex(my_rid)} or above", name)


def _log_refusal_once(reason: str) -> None:
    """Clients poll often (the pill every second): say why once, not every time."""
    if reason not in _REFUSALS_LOGGED:
        _REFUSALS_LOGGED.add(reason)
        logger.warning("voice control channel refused: %s", reason)


def _hex(rid) -> str:
    return "unknown" if rid is None else hex(rid)


def _win_connect(name: str, timeout) -> PipeConnection:
    api = _api()
    ct = api.ctypes
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    while True:
        h = api.CreateFileW(name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING,
                            _FILE_FLAG_OVERLAPPED | _SECURITY_SQOS_PRESENT
                            | _SECURITY_IDENTIFICATION, None)
        if _handle_ok(h):
            break
        err = ct.get_last_error()
        if err == _ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(2, "no control pipe (nothing is listening)", name)
        if err != _ERROR_PIPE_BUSY:
            raise _winerr(api, err)
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise socket.timeout("timed out")
        if not api.WaitNamedPipeW(name, _ms(remaining)):
            err = ct.get_last_error()
            if err == _ERROR_SEM_TIMEOUT:
                raise socket.timeout("timed out")
            if err == _ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(2, "no control pipe (nothing is listening)", name)
            raise _winerr(api, err or 6)  # anything else: do not spin
        # WaitNamedPipeW said an instance is free; loop and check the deadline,
        # since another client may take it first.
    conn = PipeConnection(h)
    try:
        _verify_server(conn, name)
    except BaseException:
        conn.close()
        raise
    conn.settimeout(timeout)
    return conn
