"""Config for the system-audio feature: defaults < JSON file < environment.

JSON file: ~/.voicemode/sysaudio.json (written by the per-OS setup; e.g. the
Windows VoiceMeeter setup records win_python / helper_path / vm_strip there).
Env overrides use the VOICEMODE_SYSAUDIO_<FIELD> convention.
"""
import json
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Optional

_ENV_PREFIX = "VOICEMODE_SYSAUDIO_"
_INT_FIELDS = {"vm_strip"}


@dataclass
class Config:
    # --- Linux (PulseAudio/PipeWire) ---
    mix_sink: str = "voicemode_mix"          # null sink voicemode records (its .monitor)
    mic_source: Optional[str] = None         # None -> default source (real mic)
    capture_monitor: Optional[str] = None    # None -> <default sink>.monitor (system output)
    # --- WSL / Windows (VoiceMeeter Remote API) ---
    win_python: Optional[str] = None         # /mnt/c/.../python.exe (WSL invokes this)
    helper_path: Optional[str] = None        # Windows path to vm_sysaudio.py
    vm_dll: Optional[str] = None             # path to VoicemeeterRemote64.dll
    vm_strip: Optional[int] = None           # VoiceMeeter strip index carrying system/Teams audio
    vm_bus: str = "B1"                       # recording bus the strip toggles into
    # --- macOS (best-effort) ---
    mac_mix_device: str = "voicemode_mix"

    @classmethod
    def default_path(cls) -> Path:
        return Path.home() / ".voicemode" / "sysaudio.json"

    @classmethod
    def load(cls, environ=None, path=None) -> "Config":
        environ = os.environ if environ is None else environ
        path = cls.default_path() if path is None else path
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            data = {}
        for f in fields(cls):
            env_key = _ENV_PREFIX + f.name.upper()
            if env_key in environ:
                data[f.name] = environ[env_key]
        valid = {f.name for f in fields(cls)}
        kwargs = {}
        for k, v in data.items():
            if k not in valid:
                continue
            if k in _INT_FIELDS and v is not None and v != "":
                v = int(v)
            kwargs[k] = v
        return cls(**kwargs)
