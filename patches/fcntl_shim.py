"""Windows shim for the POSIX `fcntl` module.

Installed as `<venv>/Lib/site-packages/fcntl.py` only on Windows by
`patches/apply.sh`. On Linux/macOS the real stdlib `fcntl` resolves first
on `sys.path` and this file is never imported.

`voice_mode.conch` uses `fcntl.flock(...)` for cross-process advisory
locking. We translate to `msvcrt.locking` on Windows.

This intentionally does NOT modify any voice-mode source — it only fills in
a missing stdlib module on Windows.
"""

import sys

if sys.platform != "win32":
    raise ImportError(
        "patches/fcntl_shim.py must only be used on Windows. "
        "On POSIX systems the stdlib fcntl module should resolve first."
    )

import msvcrt

# Match the integer values of POSIX fcntl flock operations
LOCK_SH = 1   # shared lock (we map to LOCK_EX since msvcrt has no shared)
LOCK_EX = 2   # exclusive lock
LOCK_NB = 4   # non-blocking (combined with LOCK_SH or LOCK_EX)
LOCK_UN = 8   # unlock


def flock(fd, op):
    """Mimic fcntl.flock using msvcrt.locking.

    POSIX flock locks the entire file. msvcrt.locking locks a byte range
    starting at the current file pointer. We lock 1 byte at offset 0,
    which is sufficient as an advisory mutex when all participants follow
    the same convention.
    """
    if op & LOCK_UN:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    mode = msvcrt.LK_NBLCK if (op & LOCK_NB) else msvcrt.LK_LOCK
    try:
        msvcrt.locking(fd, mode, 1)
    except OSError as e:
        # Translate to BlockingIOError to match POSIX flock semantics on
        # non-blocking failures
        raise BlockingIOError(e.errno, str(e))


def fcntl(fd, op, arg=0):
    """Stub — voice_mode doesn't use fcntl.fcntl(). Raise on use."""
    raise NotImplementedError("fcntl.fcntl is not implemented on Windows")
