"""Is an NVIDIA GPU usable for the CUDA speech-recognition build? One read-only check.

`probe()` runs `nvidia-smi --query-gpu=name,driver_version --format=csv,noheader` once (5 s at
most, no window on Windows) and returns a Result; `summary()` turns it into the one JSON shape
`python -m vml_supervisor.gpu` prints:

    {"available": bool, "reason": code, "detail": str, "gpu": str|null, "driver": str|null}

Codes: ok, no_nvidia_smi, no_gpu_listed, driver_too_old, nvidia_smi_failed, nvidia_smi_timeout,
not_applicable (macOS: no NVIDIA CUDA there, nothing is run). Only "ok" is available. The CUDA
12.4 build needs NVIDIA's documented minimum driver for CUDA 12.x: >= 527.41 on Windows and
>= 525.60.13 on Linux (minor-version compatibility). WSL runs on the WINDOWS driver, so the
Windows minimum applies there. The whole version is compared, not its major number; a version
that cannot be read is nvidia_smi_failed and never available. Never downloads, installs or
starts anything.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from .result import Result

TIMEOUT = 5            # nvidia-smi -L answered in about 0.2 s on the Windows laptop (2026-11-17, n=2)
MIN_DRIVER_WINDOWS = (527, 41)      # CUDA 12.x minimum on Windows, and on WSL (the Windows driver)
MIN_DRIVER_LINUX = (525, 60, 13)    # CUDA 12.x minimum on Linux
QUERY = ["--query-gpu=name,driver_version", "--format=csv,noheader"]


def _first_line(*texts):
    for text in texts:
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()
    return ""


def _read_text(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def is_wsl(platform=None, read=_read_text) -> bool:
    """Is this Linux running under WSL (so the Windows driver is what nvidia-smi reports)?"""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in read("/proc/sys/kernel/osrelease").lower()
    except OSError:
        return False


def min_driver(platform, wsl):
    """The minimum driver version (a tuple) for this platform."""
    return MIN_DRIVER_WINDOWS if platform == "win32" or wsl else MIN_DRIVER_LINUX


def probe(run=subprocess.run, which=shutil.which, platform=None, wsl=None) -> Result:
    """The GPU check: Result(code, detail, {"gpu": name|None, "driver": version|None})."""
    platform = sys.platform if platform is None else platform
    if wsl is None:
        wsl = is_wsl(platform)
    none = {"gpu": None, "driver": None}
    if platform == "darwin":
        return Result("not_applicable", "macOS has no NVIDIA CUDA", none)
    smi = which("nvidia-smi")
    if not smi:
        return Result("no_nvidia_smi", "nvidia-smi is not on PATH", none)
    options = {}
    if platform == "win32":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        done = run([smi, *QUERY], capture_output=True, text=True, errors="replace",
                   timeout=TIMEOUT, stdin=subprocess.DEVNULL, **options)
    except subprocess.TimeoutExpired:
        return Result("nvidia_smi_timeout", f"nvidia-smi did not answer in {TIMEOUT} s", none)
    except OSError as error:
        return Result("nvidia_smi_failed", f"nvidia-smi could not run: {error}", none)
    first = _first_line(done.stdout, done.stderr)
    if done.returncode != 0:
        return Result("nvidia_smi_failed", f"nvidia-smi exited {done.returncode}"
                      + (f": {first}" if first else ""), none)
    rows = [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]
    if not rows:
        return Result("no_gpu_listed", "nvidia-smi lists no GPU", none)
    name, _, driver = (part.strip() for part in rows[0].rpartition(","))
    parts = re.match(r"\d+(?:\.\d+)*$", driver)
    if not name or not parts:
        return Result("nvidia_smi_failed", f"nvidia-smi answered an unreadable line: {rows[0]}", none)
    found = {"gpu": name, "driver": driver}
    needed = min_driver(platform, wsl)
    if tuple(int(n) for n in driver.split(".")) < needed:
        minimum = ".".join(str(n) for n in needed)
        where = "Windows or WSL" if needed == MIN_DRIVER_WINDOWS else "Linux"
        return Result("driver_too_old", f"driver {driver} is older than {minimum}, the minimum "
                      f"for the CUDA 12.4 build on {where}", found)
    return Result("ok", f"{name}, driver {driver}", found)


def summary(result: Result) -> dict:
    """The one JSON shape: available, reason (the code), detail, gpu, driver."""
    return {"available": result.code == "ok", "reason": result.code, "detail": result.detail,
            "gpu": result.data.get("gpu"), "driver": result.data.get("driver")}


def main() -> int:
    print(json.dumps(summary(probe())), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
