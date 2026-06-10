"""WSL backend — delegate to a Windows-side VoiceMeeter helper over interop.

WSL cannot see Windows app audio; the mix is assembled on Windows by VoiceMeeter
and exposed as the default recording device (which WSL receives as RDPSource).
The on/off toggle flips the system/Teams strip into VoiceMeeter's recording bus
by calling a small Windows helper (vm_sysaudio.py) through the Windows Python.

NOTE: win_python may be referenced by its /mnt/c/... path (WSL resolves the
binary), but helper_path MUST be a Windows path (C:\\...), because the Windows
python process resolves it. Both are recorded by the Windows setup into
~/.voicemode/sysaudio.json.
"""
import subprocess

from ..model import Result


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return (p.returncode, p.stdout.strip(), p.stderr.strip())


def apply(state, cfg, run=None):
    run = run or _run
    if not (cfg.win_python and cfg.helper_path):
        return Result(ok=False, state="unknown",
                      detail=("WSL backend not configured: run the Windows VoiceMeeter "
                              "setup, then set win_python + helper_path (and vm_strip) "
                              "in ~/.voicemode/sysaudio.json"),
                      platform="wsl")
    cmd = [cfg.win_python, cfg.helper_path, state]
    if cfg.vm_strip is not None:
        cmd.append(str(cfg.vm_strip))
    rc, out, err = run(cmd)
    reported = out.strip().splitlines()[-1].strip() if out.strip() else ""
    resolved = reported if reported in ("on", "off") else state
    return Result(ok=(rc == 0), state=resolved if rc == 0 else "unknown",
                  detail=(out or err).strip(), platform="wsl")
