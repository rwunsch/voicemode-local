"""Linux backend — PulseAudio/PipeWire, no third-party software.

Mix model (created by `setup`, healed by `on`):
    null sink `voicemode_mix`
      <- module-loopback from the real mic            (always on)
      <- module-loopback from <system output>.monitor (toggled by on/off)
    voicemode records `voicemode_mix.monitor`.

The system-output tap is non-destructive: you keep hearing the audio on your
real speakers while it is also folded into the voice input.
"""
import subprocess

from ..model import Result


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return (p.returncode, p.stdout.strip(), p.stderr.strip())


def _short_modules(run):
    rc, out, _ = run(["pactl", "list", "short", "modules"])
    return out if rc == 0 else ""


def _find_module(modules_text, module_name, *arg_needles):
    """Return the id of the first loaded module matching name + all arg needles."""
    for line in modules_text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        idx, name = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ""
        if name == module_name and all(n in args for n in arg_needles):
            return idx
    return None


def _default_sink(run):
    rc, out, _ = run(["pactl", "get-default-sink"])
    return out if rc == 0 and out else "@DEFAULT_SINK@"


def _default_source(run):
    rc, out, _ = run(["pactl", "get-default-source"])
    return out if rc == 0 and out else "@DEFAULT_SOURCE@"


def _monitor(cfg, run):
    return cfg.capture_monitor or (_default_sink(run) + ".monitor")


def ensure_mix(cfg, run):
    if not _find_module(_short_modules(run), "module-null-sink",
                        f"sink_name={cfg.mix_sink}"):
        run(["pactl", "load-module", "module-null-sink",
             f"sink_name={cfg.mix_sink}",
             f"sink_properties=device.description={cfg.mix_sink}"])
    mic = cfg.mic_source or _default_source(run)
    if mic == f"{cfg.mix_sink}.monitor":
        return  # never loop the mix into itself
    if not _find_module(_short_modules(run), "module-loopback",
                        f"source={mic}", f"sink={cfg.mix_sink}"):
        run(["pactl", "load-module", "module-loopback",
             f"source={mic}", f"sink={cfg.mix_sink}", "latency_msec=20"])


def turn_on(cfg, run):
    ensure_mix(cfg, run)
    mon = _monitor(cfg, run)
    if not _find_module(_short_modules(run), "module-loopback",
                        f"source={mon}", f"sink={cfg.mix_sink}"):
        rc, _out, err = run(["pactl", "load-module", "module-loopback",
                             f"source={mon}", f"sink={cfg.mix_sink}",
                             "latency_msec=20"])
        if rc != 0:
            return Result(ok=False, state="off",
                          detail=f"failed to tap {mon}: {err}", platform="linux")
    return Result(ok=True, state="on",
                  detail=f"system output ({mon}) folded into {cfg.mix_sink}",
                  platform="linux")


def turn_off(cfg, run):
    mon = _monitor(cfg, run)
    mid = _find_module(_short_modules(run), "module-loopback",
                       f"source={mon}", f"sink={cfg.mix_sink}")
    if mid:
        run(["pactl", "unload-module", mid])
    return Result(ok=True, state="off",
                  detail="system-output tap removed (mic only)", platform="linux")


def status(cfg, run):
    mon = _monitor(cfg, run)
    on = bool(_find_module(_short_modules(run), "module-loopback",
                           f"source={mon}", f"sink={cfg.mix_sink}"))
    return Result(ok=True, state="on" if on else "off",
                  detail=f"capture sink {cfg.mix_sink}", platform="linux")


def setup(cfg, run):
    ensure_mix(cfg, run)
    run(["pactl", "set-default-source", f"{cfg.mix_sink}.monitor"])
    return Result(ok=True, state=status(cfg, run).state,
                  detail=(f"default source -> {cfg.mix_sink}.monitor; "
                          "restart voicemode to capture it"), platform="linux")


def teardown(cfg, run):
    mic = cfg.mic_source or _default_source(run)
    mon = _monitor(cfg, run)
    for needles in ((f"source={mon}", f"sink={cfg.mix_sink}"),
                    (f"sink={cfg.mix_sink}",)):
        mid = _find_module(_short_modules(run), "module-loopback", *needles)
        if mid:
            run(["pactl", "unload-module", mid])
    nid = _find_module(_short_modules(run), "module-null-sink",
                       f"sink_name={cfg.mix_sink}")
    if nid:
        run(["pactl", "unload-module", nid])
    if mic and mic != f"{cfg.mix_sink}.monitor":
        run(["pactl", "set-default-source", mic])
    return Result(ok=True, state="off", detail="sysaudio mix torn down",
                  platform="linux")


_STATES = {"on": turn_on, "off": turn_off, "status": status,
           "setup": setup, "teardown": teardown}


def apply(state, cfg, run=None):
    run = run or _run
    fn = _STATES.get(state)
    if fn is None:
        return Result(ok=False, state="unknown",
                      detail=f"unknown state {state!r}", platform="linux")
    return fn(cfg, run)
