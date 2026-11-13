"""Named hooks: how a harness extends the core through its profile.

A profile maps hook names to "module:function" entry points. The host loads
them once at start (load_hooks) and keeps running on what it loaded, so an
updater replacing the hook folder cannot change a running host. Hooks take and
return plain data. Nothing here raises: a failure is a Result and the default
for that hook is used.

Hook contract
- sys.path is restored after loading, so a hook module must import its siblings
  at module import time, not lazily inside the function.
- Hook modules are cached in sys.modules under their own names: name them
  uniquely (for example felix_voice_profile).
- A hook that raises or calls sys.exit gives Result("hook_failed") and the
  default; KeyboardInterrupt still propagates. A return of the wrong shape gives
  Result("hook_bad_return") and the default.
- service_command returns [argv, marker]. The core owns a service only while its command line
  holds the marker and `--port N` (procs.owned), so the marker must be non-empty and appear in
  argv, and argv must hold "--port" followed by str(port) as two separate arguments (not
  "--port=N", not "-p N"). Otherwise ensure_services refuses to start it:
  Result("service_command_bad") for an empty marker, Result("service_command_unowned") for the rest.
- read_mode returns the helper's mode (a string) or None (its default). "off", in any case, means
  the helper must not run: the host and host.ensure stop it (stop_helper) instead of starting it.
  When read_mode fails, the helper is left as it is, running or not (the host says so once).
- A pre_start hook that raises does NOT block the start: run_pre_start returns
  the hook_failed Result and the caller passes it to on_result. Only returned
  refusal text blocks, as Result("pre_start_refused").
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from .profile import HOOK_NAMES, HostProfile
from .result import Result


@dataclass
class Hooks:
    """The resolved hook callables, by name."""
    funcs: dict[str, Callable] = field(default_factory=dict)


def load_hooks(profile: HostProfile) -> tuple[Hooks, list[Result]]:
    hooks = Hooks()
    results: list[Result] = []
    saved = list(sys.path)
    sys.path[:0] = [str(p) for p in profile.hook_paths]
    try:
        for name, entry in profile.hooks.items():
            if name not in HOOK_NAMES:
                continue  # reported by HostProfile.validate(), which ensure and host_main call
            try:
                module, _, func = entry.partition(":")
                if not module or not func:
                    raise ValueError(f"entry point {entry!r} is not 'module:function'")
                fn = getattr(importlib.import_module(module), func)
                if not callable(fn):
                    raise TypeError(f"{entry!r} is not callable")
                hooks.funcs[name] = fn
            except Exception as exc:  # noqa: BLE001 - a bad hook must not stop the others
                results.append(Result("hook_failed", f"{name}: {exc!r}", {"hook": name}))
    finally:
        sys.path[:] = saved
    return hooks, results


def call_hook(hooks: Hooks, name: str, *args, default: Any = None) -> tuple[Any, Result | None]:
    fn = hooks.funcs.get(name)
    if fn is None:
        return default, None
    try:
        return fn(*args), None
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        return default, Result("hook_failed", f"{name}: {exc!r}", {"hook": name})


# -- defaults and typed wrappers (profile in, plain data to the hook) ----------

def _is_str_or_none(v) -> bool:
    return v is None or isinstance(v, str)


def _checked(hooks: Hooks, name: str, args: tuple, ok: Callable[[Any], bool],
             default: Callable[[], Any]):
    """call_hook plus a shape check; the default is computed only when needed."""
    if name not in hooks.funcs:
        return default(), None
    val, res = call_hook(hooks, name, *args, default=_MISSING)
    if res is not None:
        return default(), res
    if not ok(val):
        return default(), Result("hook_bad_return", f"{name}: returned {type(val).__name__}",
                                 {"hook": name})
    return val, None


_MISSING = object()


def resolve_runtime(hooks: Hooks, profile: HostProfile):
    return _checked(hooks, "resolve_runtime", (profile.to_dict(),),
                    lambda v: isinstance(v, list) and len(v) == 2
                    and isinstance(v[0], str) and isinstance(v[1], bool),
                    lambda: [str(profile.runtime_dir), True])


def environment(hooks: Hooks, profile: HostProfile, runtime: str, ports: dict, modes: dict):
    return _checked(hooks, "environment", (profile.to_dict(), runtime, ports, modes),
                    lambda v: isinstance(v, dict)
                    and all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()),
                    lambda: dict(profile.env))


def default_service_command(profile: HostProfile, kind: str, port: int) -> list:
    spec = profile.services[kind]
    return [[a.replace("{port}", str(port)) for a in spec.argv], spec.marker]


def service_command(hooks: Hooks, profile: HostProfile, kind: str, runtime: str, port: int):
    return _checked(hooks, "service_command", (profile.to_dict(), kind, runtime, port),
                    lambda v: isinstance(v, list) and len(v) == 2 and isinstance(v[1], str)
                    and isinstance(v[0], list) and all(isinstance(a, str) for a in v[0]),
                    lambda: default_service_command(profile, kind, port))


def prepare_home(hooks: Hooks, profile: HostProfile, env: dict):
    return call_hook(hooks, "prepare_home", profile.to_dict(), env)


def read_mode(hooks: Hooks, profile: HostProfile, kind: str):
    return _checked(hooks, "read_mode", (profile.to_dict(), kind), _is_str_or_none, lambda: None)


def on_result(hooks: Hooks, profile: HostProfile, results: list[dict]):
    return call_hook(hooks, "on_result", profile.to_dict(), results)


def run_pre_start(hooks: Hooks, profile: HostProfile, runtime: str, env: dict) -> Result | None:
    """None allows the start. hook_failed / hook_bad_return are returned but do not
    block (the caller records them via on_result); only refusal text blocks."""
    text, res = _checked(hooks, "pre_start", (profile.to_dict(), runtime, env),
                         _is_str_or_none, lambda: None)
    if res:
        return res
    return Result("pre_start_refused", text) if text else None
