"""Named hooks: how a harness extends the core through its profile.

A profile maps hook names to "module:function" entry points. The host loads
them once at start (load_hooks) and keeps running on what it loaded, so an
updater replacing the hook folder cannot change a running host. Hooks take and
return plain data. Nothing here raises: a failure is a Result and the default
for that hook is used.
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
                continue  # reported by HostProfile.validate()
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
    except Exception as exc:  # noqa: BLE001
        return default, Result("hook_failed", f"{name}: {exc!r}", {"hook": name})


# -- defaults and typed wrappers (profile in, plain data to the hook) ----------

def resolve_runtime(hooks: Hooks, profile: HostProfile):
    return call_hook(hooks, "resolve_runtime", profile.to_dict(),
                     default=[str(profile.runtime_dir), True])


def environment(hooks: Hooks, profile: HostProfile, runtime: str, ports: dict, modes: dict):
    return call_hook(hooks, "environment", profile.to_dict(), runtime, ports, modes,
                     default=dict(profile.env))


def default_service_command(profile: HostProfile, kind: str, port: int) -> list:
    spec = profile.services[kind]
    return [[a.replace("{port}", str(port)) for a in spec.argv], spec.marker]


def service_command(hooks: Hooks, profile: HostProfile, kind: str, runtime: str, port: int):
    return call_hook(hooks, "service_command", profile.to_dict(), kind, runtime, port,
                     default=default_service_command(profile, kind, port))


def prepare_home(hooks: Hooks, profile: HostProfile, env: dict):
    return call_hook(hooks, "prepare_home", profile.to_dict(), env)


def read_mode(hooks: Hooks, profile: HostProfile, kind: str):
    return call_hook(hooks, "read_mode", profile.to_dict(), kind)


def on_result(hooks: Hooks, profile: HostProfile, results: list[dict]):
    return call_hook(hooks, "on_result", profile.to_dict(), results)


def run_pre_start(hooks: Hooks, profile: HostProfile, runtime: str, env: dict) -> Result | None:
    """None allows the start; a Result (refused or hook_failed) is for the caller to record."""
    text, failed = call_hook(hooks, "pre_start", profile.to_dict(), runtime, env)
    if failed:
        return failed
    return Result("pre_start_refused", text) if text else None
