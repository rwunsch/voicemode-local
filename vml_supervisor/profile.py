"""HostProfile: everything a host needs to describe its voice processes."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path

from .result import Result

# Ports the local voice stack owns regardless of profile.
BASE_RESERVED_PORTS = (8880, 8881, 8882, 2022, 9000)

# Profile file format version this package understands (a newer one is refused).
SCHEMA_VERSION = 1
# The hooks a profile may name (each a plain-data function, see hooks.py).
HOOK_NAMES = ("resolve_runtime", "environment", "service_command", "prepare_home",
              "pre_start", "read_mode", "on_result")


@dataclass(frozen=True)
class ServiceSpec:
    argv: list[str]  # may contain the placeholder "{port}"
    marker: str
    port_range: tuple[int, int]
    ready_path: str
    ready_kind: str  # "kokoro" | "whisper"


@dataclass(frozen=True)
class HelperSpec:
    argv: list[str]
    marker: str
    log: str
    platforms: tuple[str, ...]
    needs_mode: bool


@dataclass(frozen=True)
class PortOverride:
    kind: str  # "file" | "env" | "none"
    path: Path | None = None
    keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostProfile:
    name: str
    root: Path
    run_dir: Path
    runtime_dir: Path
    python: Path
    working_dir: Path
    env: dict[str, str]
    services: dict[str, ServiceSpec]  # keys: tts, stt
    helpers: dict[str, HelperSpec]  # keys: barge, ptt
    reserved_ports: tuple[int, ...]
    port_override: PortOverride
    start_timeout: float
    stop_timeout: float
    watch_interval: float
    restart_limit: tuple[int, float]  # (count, window seconds)
    lease_markers: tuple[str, ...]
    voice_server_marker: str
    host_command: list[str]
    host_marker: str
    watch_command: list[str]
    watch_marker: str
    job_name: str
    host_start_grace: float = 60.0
    schema_version: int = SCHEMA_VERSION
    # hook name -> "module:function"; see vml_supervisor.hooks
    hooks: dict[str, str] = field(default_factory=dict)
    hook_paths: tuple[Path, ...] = ()

    # -- serialisation -------------------------------------------------
    def to_dict(self) -> dict:
        return _encode(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HostProfile":
        return _decode(cls, d)

    def to_file(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def from_file(cls, path: Path) -> "HostProfile":
        return cls.from_dict(json.loads(Path(path).read_text()))

    # -- validation ----------------------------------------------------
    def validate(self) -> list[Result]:
        out: list[Result] = []
        reserved = set(BASE_RESERVED_PORTS) | set(self.reserved_ports)
        ranges = {k: s.port_range for k, s in self.services.items()}
        for key, (lo, hi) in ranges.items():
            hit = sorted(p for p in reserved if lo <= p <= hi)
            if hit:
                out.append(Result("port_range_reserved",
                                  f"{key} range {lo}-{hi} contains reserved {hit}",
                                  {"service": key, "ports": hit}))
        keys = sorted(ranges)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                (alo, ahi), (blo, bhi) = ranges[a], ranges[b]
                if alo <= bhi and blo <= ahi:
                    out.append(Result("port_ranges_overlap",
                                      f"{a} {alo}-{ahi} overlaps {b} {blo}-{bhi}",
                                      {"services": [a, b]}))
        for name in sorted(self.hooks):
            if name not in HOOK_NAMES:
                out.append(Result("hook_unknown", name, {"hook": name}))
        return out


_DATACLASSES = {c.__name__: c for c in (ServiceSpec, HelperSpec, PortOverride)}


def _encode(v):
    if hasattr(v, "__dataclass_fields__"):
        return {f.name: _encode(getattr(v, f.name)) for f in fields(v)}
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {k: _encode(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_encode(x) for x in v]
    return v


def _tup(v):
    return tuple(v)


def _decode(cls, d: dict):
    """Rebuild a dataclass from its encoded dict using the declared field types."""
    kw = {}
    hints = {f.name: str(f.type) for f in fields(cls)}
    for name, t in hints.items():
        if name not in d:
            continue
        v = d[name]
        if v is None:
            kw[name] = None
        elif t.startswith("tuple[Path"):
            kw[name] = tuple(Path(x) for x in v)
        elif "Path" in t and "dict" not in t:
            kw[name] = Path(v)
        elif t.startswith("dict[str, ServiceSpec"):
            kw[name] = {k: _decode(ServiceSpec, x) for k, x in v.items()}
        elif t.startswith("dict[str, HelperSpec"):
            kw[name] = {k: _decode(HelperSpec, x) for k, x in v.items()}
        elif t == "PortOverride":
            kw[name] = _decode(_DATACLASSES[t], v)
        elif t.startswith("tuple"):
            kw[name] = _tup(v)
        else:
            kw[name] = v
    return cls(**kw)


def load_profile(path: Path) -> tuple[HostProfile | None, list[Result]]:
    """Read a profile file without raising: (profile, []) or (None, [Result])."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        return None, [Result("profile_malformed", f"{path}: {exc!r}")]
    if not isinstance(d, dict):
        return None, [Result("profile_malformed", f"{path}: not a JSON object")]
    version = d.get("schema_version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        return None, [Result("profile_malformed", f"{path}: schema_version {version!r}")]
    if version > SCHEMA_VERSION:
        return None, [Result("profile_schema_unsupported",
                             f"{path}: schema_version {version} > supported {SCHEMA_VERSION}",
                             {"found": version, "supported": SCHEMA_VERSION})]
    try:
        return HostProfile.from_dict(d), []
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return None, [Result("profile_malformed", f"{path}: {exc!r}")]
