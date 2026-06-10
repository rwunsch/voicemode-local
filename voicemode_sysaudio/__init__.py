"""voicemode_sysaudio — fold system output audio into voicemode's voice input.

One capability, per-OS backend:
  - linux  : PulseAudio/PipeWire null-sink + module-loopback (no extra software)
  - wsl    : delegate to a Windows-side VoiceMeeter helper over interop
  - windows: VoiceMeeter Remote API (ctypes)
  - darwin : BlackHole + device switching (best-effort)

States: on | off | status | setup | teardown.
"""
from .config import Config
from .detect import platform_kind
from .model import Result

__all__ = ["Config", "Result", "platform_kind", "get_backend",
           "set_system_audio", "cli"]

_VALID_STATES = ("on", "off", "status", "setup", "teardown")


def get_backend(kind):
    if kind == "wsl":
        from .backends import wsl as b
    elif kind == "windows":
        from .backends import windows as b
    elif kind == "darwin":
        from .backends import darwin as b
    else:
        from .backends import linux as b
    return b


def set_system_audio(state, cfg=None, kind=None) -> Result:
    cfg = cfg if cfg is not None else Config.load()
    kind = kind if kind is not None else platform_kind()
    r = get_backend(kind).apply(state, cfg)
    if not r.platform:
        r.platform = kind
    return r


def cli(argv=None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    state = argv[0] if argv else "status"
    if state not in _VALID_STATES:
        print(f"usage: voicemode-switch sysaudio {{{'|'.join(_VALID_STATES)}}}")
        return 2
    r = set_system_audio(state)
    suffix = f" — {r.detail}" if r.detail else ""
    print(f"[sysaudio:{r.platform}] system audio {r.state}{suffix}")
    return 0 if r.ok else 1
