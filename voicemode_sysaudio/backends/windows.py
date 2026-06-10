"""Native Windows backend — VoiceMeeter Remote API via ctypes.

Flips the system/Teams strip into/out of the recording bus (default B1) by
setting the VoiceMeeter parameter Strip[N].B1 to 1.0/0.0. This is the same
operation the WSL helper (vm_sysaudio.py) performs; this module is used when
voicemode runs natively on Windows.
"""
from ..model import Result

_DEFAULT_DLL = r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote64.dll"


def param_name(cfg) -> str:
    return f"Strip[{cfg.vm_strip}].{cfg.vm_bus}"


def apply(state, cfg, run=None):
    if cfg.vm_strip is None:
        return Result(ok=False, state="unknown",
                      detail="not configured: set vm_strip (VoiceMeeter strip index)",
                      platform="windows")
    try:
        import ctypes
    except Exception as e:  # pragma: no cover - non-Windows
        return Result(ok=False, state="unknown", detail=str(e), platform="windows")
    pname = param_name(cfg).encode("ascii")
    try:
        vm = ctypes.WinDLL(cfg.vm_dll or _DEFAULT_DLL)
        if vm.VBVMR_Login() < 0:
            return Result(ok=False, state="unknown",
                          detail="VBVMR_Login failed (is VoiceMeeter running?)",
                          platform="windows")
        try:
            if state == "status":
                val = ctypes.c_float()
                vm.VBVMR_GetParameterFloat(pname, ctypes.byref(val))
                resolved = "on" if val.value >= 0.5 else "off"
            else:
                target = 1.0 if state == "on" else 0.0
                vm.VBVMR_SetParameterFloat(pname, ctypes.c_float(target))
                resolved = state
        finally:
            vm.VBVMR_Logout()
        return Result(ok=True, state=resolved, detail=param_name(cfg),
                      platform="windows")
    except Exception as e:  # pragma: no cover - non-Windows
        return Result(ok=False, state="unknown", detail=str(e), platform="windows")
