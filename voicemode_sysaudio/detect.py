"""Platform detection for the system-audio backend dispatch.

Returns one of: "wsl" | "linux" | "windows" | "darwin". WSL is distinguished
from native Linux because its audio path (WSLg RDP bridge) cannot see Windows
app audio — it must delegate the mix/toggle to a Windows-side helper.
"""
import os
import sys
from pathlib import Path


def platform_kind(sys_platform=None, uname_release=None, environ=None):
    sys_platform = sys.platform if sys_platform is None else sys_platform
    environ = os.environ if environ is None else environ
    if sys_platform == "win32":
        return "windows"
    if sys_platform == "darwin":
        return "darwin"
    # linux family — is it WSL?
    rel = uname_release
    if rel is None:
        try:
            rel = Path("/proc/sys/kernel/osrelease").read_text()
        except OSError:
            rel = ""
    if "microsoft" in (rel or "").lower() or environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    return "linux"
