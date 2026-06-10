"""Windows shim for the POSIX `resource` module.

Installed as `<venv>/Lib/site-packages/resource.py` only on Windows by
`patches/apply.sh`. On Linux/macOS the real stdlib `resource` resolves first
on `sys.path` and this file is never imported.

`voice_mode.tools.converse` uses `resource.getrusage(RUSAGE_SELF).ru_maxrss`
to log peak memory in DEBUG mode. We approximate via `psutil` when available,
falling back to 0.

This intentionally does NOT modify any voice-mode source — it only fills in
a missing stdlib module on Windows.
"""

import sys

if sys.platform != "win32":
    raise ImportError(
        "patches/resource_shim.py must only be used on Windows. "
        "On POSIX systems the stdlib resource module should resolve first."
    )

# Constants used by callers
RUSAGE_SELF = 0
RUSAGE_CHILDREN = -1


class _RUsage:
    """Mimics the rusage struct returned by getrusage."""

    def __init__(self, ru_maxrss=0):
        # Only the fields voice_mode actually reads are populated. POSIX
        # getrusage returns many more — they're 0 here.
        self.ru_utime = 0.0
        self.ru_stime = 0.0
        self.ru_maxrss = ru_maxrss
        self.ru_ixrss = 0
        self.ru_idrss = 0
        self.ru_isrss = 0
        self.ru_minflt = 0
        self.ru_majflt = 0
        self.ru_nswap = 0
        self.ru_inblock = 0
        self.ru_oublock = 0
        self.ru_msgsnd = 0
        self.ru_msgrcv = 0
        self.ru_nsignals = 0
        self.ru_nvcsw = 0
        self.ru_nivcsw = 0


def getrusage(who=RUSAGE_SELF):
    """Approximate ru_maxrss (peak resident set in KB) via psutil if available."""
    rss_kb = 0
    try:
        import psutil
        # POSIX ru_maxrss is in KB on Linux. memory_info().rss is bytes.
        rss_kb = psutil.Process().memory_info().rss // 1024
    except Exception:
        pass
    return _RUsage(ru_maxrss=rss_kb)
