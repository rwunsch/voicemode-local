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
* the client opens with ``SECURITY_IDENTIFICATION`` (the server cannot
  impersonate it) and refuses to send anything unless the pipe's owner SID is
  its own.

Naming
======
``\\\\.\\pipe\\voicemode-control-<first 16 hex of sha256(path)>`` where path is
the normalised absolute ``control.sock`` path, so each install's base dir gets
its own pipe. The name is derived on every call and never cached: FELIX moves
``control.sock`` whenever it rebuilds a runtime.

On POSIX nothing here is used; importing the module loads no Windows API.
Installed into voice_mode/control_transport.py by patches/apply.sh.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import sys
import threading

PIPE_PREFIX = "\\\\.\\pipe\\voicemode-control-"

_API = None  # Windows API bindings, loaded on first use


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


def owner_only_sddl(sid: str) -> str:
    """Owner and sole grantee is ``sid``; ``P`` blocks inherited ACEs."""
    return f"O:{sid}D:P(A;;GA;;;{sid})"


def sddl_is_owner_only(sddl: str, sid: str) -> bool:
    """True if a read-back descriptor is owned by ``sid``, protected, and has
    exactly one ACE: an allow for ``sid``."""
    m = re.fullmatch(r"O:(?P<o>[^:]+?)(?:G:[^:]+?)?D:(?P<flags>[A-Z]*)(?P<aces>(?:\([^)]*\))*)", sddl)
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


def current_user_sid() -> str:
    """String SID of the user this process runs as (the token's user)."""
    api = _api()
    ct = api.ctypes
    tok = api.w.HANDLE()
    if not api.OpenProcessToken(api.GetCurrentProcess(), _TOKEN_QUERY, ct.byref(tok)):
        raise _winerr(api)
    try:
        size = api.w.DWORD()
        api.GetTokenInformation(tok, _TOKEN_USER, None, 0, ct.byref(size))
        buf = ct.create_string_buffer(size.value)
        if not api.GetTokenInformation(tok, _TOKEN_USER, buf, size, ct.byref(size)):
            raise _winerr(api)
        psid = ct.cast(buf, ct.POINTER(ct.c_void_p))[0]  # TOKEN_USER.User.Sid
        return _sid_string(api, psid)
    finally:
        api.CloseHandle(tok)


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
    """Owner and DACL of the pipe behind a connection, read back from the handle."""
    api = _api()
    return _security_info(api, conn._handle,
                          _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION)[1]


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
        self._api = _api()
        self._handle = handle
        self._ov = _overlapped(self._api)
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
        self._handle = None
        self._sid = current_user_sid()
        sd = api.ctypes.c_void_p()
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                owner_only_sddl(self._sid), _SDDL_REVISION_1, api.ctypes.byref(sd), None):
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
            if self._handle is None:  # a previous hand-over could not re-arm
                self._handle = self._new_instance(first=False)
            if not self._pending:
                ok = api.ConnectNamedPipe(self._handle, ct.byref(self._ov))
                err = 0 if ok else ct.get_last_error()
                # Connected before we asked (PIPE_CONNECTED), or connected,
                # wrote and closed already (NO_DATA): either way hand it over,
                # so a fire-and-forget command still in the buffer is read.
                if err in (_ERROR_PIPE_CONNECTED, _ERROR_NO_DATA):
                    return self._hand_over()
                if err != _ERROR_IO_PENDING:
                    raise _winerr(api, err or 6)
                self._pending = True
            r = api.WaitForSingleObject(self._ov.hEvent, _ms(self._timeout))
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


def _win_connect(name: str, timeout) -> PipeConnection:
    api = _api()
    ct = api.ctypes
    import time
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
    conn = PipeConnection(h)
    try:
        owner, me = pipe_owner_sid(conn), current_user_sid()
        if owner != me:
            raise PermissionError(5, f"control pipe owner {owner} is not the current user {me}",
                                  name)
    except BaseException:
        conn.close()
        raise
    conn.settimeout(timeout)
    return conn
