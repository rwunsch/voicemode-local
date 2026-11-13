"""HostProfile: everything a host needs to describe its voice processes."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path

from .result import Result

# Ports the local voice stack owns regardless of profile.
BASE_RESERVED_PORTS = (8880, 8881, 8882, 2022, 9000)

# Profile file format version this package understands (a newer one is refused).
SCHEMA_VERSION = 1
# The hooks a profile may name (each a plain-data function, see hooks.py).
HOOK_NAMES = ("resolve_runtime", "environment", "service_command", "prepare_home",
              "pre_start", "read_mode", "on_result")
# The services every profile has, and the readiness answers check_service understands.
SERVICE_KINDS = ("tts", "stt")
READY_KINDS = ("kokoro", "whisper")
# validate() findings that refuse a start (ensure and the host); hook_unknown is only reported.
BLOCKING = ("port_range_reserved", "port_ranges_overlap", "port_range_bad", "service_missing",
            "ready_kind_unknown")


def blocking(results) -> bool:
    """True when validate()'s findings include one that must refuse a start."""
    return any(r.code in BLOCKING for r in results)


class ProfileError(ValueError):
    """A profile that cannot be built; `result` is the recorded refusal (never a bare TypeError)."""

    def __init__(self, result: Result):
        super().__init__(result.detail)
        self.result = result


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
    # Optional: the command that prints the helper's own status line (FELIX-voice point 8).
    status_argv: list[str] = field(default_factory=list)


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
        version = d.get("schema_version", 1)
        if isinstance(version, int) and not isinstance(version, bool) and version > SCHEMA_VERSION:
            raise ValueError(f"unsupported profile schema_version {version} > {SCHEMA_VERSION}")
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
    def from_file(cls, path: Path, results: list | None = None) -> "HostProfile":
        """Read a profile. A missing key raises ProfileError (profile_malformed, naming the key). A key
        this version does not know is a Result(profile_key_unknown) appended to `results`; with no
        `results` list to receive it, it raises ProfileError instead of passing silently."""
        d = json.loads(Path(path).read_text())
        profile = cls.from_dict(d)
        for finding in unknown_keys(d):
            if results is None:
                raise ProfileError(finding)
            results.append(finding)
        return profile

    # -- validation ----------------------------------------------------
    def validate(self) -> list[Result]:
        """Findings about the profile itself. ensure() and host_main() call it and refuse to start
        on any code in BLOCKING; hook_unknown is reported and the hook is ignored."""
        out: list[Result] = []
        for kind in SERVICE_KINDS:
            if kind not in self.services:
                out.append(Result("service_missing", f"the profile has no {kind!r} service", {"service": kind}))
        for key, spec in self.services.items():
            if spec.ready_kind not in READY_KINDS:
                out.append(Result("ready_kind_unknown", f"{key}: ready_kind {spec.ready_kind!r} is not one of "
                                  f"{', '.join(READY_KINDS)}", {"service": key, "ready_kind": spec.ready_kind}))
        reserved = set(BASE_RESERVED_PORTS) | set(self.reserved_ports)
        ranges = {}
        for key, spec in self.services.items():
            lo, hi = spec.port_range
            if not (isinstance(lo, int) and isinstance(hi, int) and 1 <= lo <= hi <= 65535):
                out.append(Result("port_range_bad", f"{key} range {lo}-{hi}: give 1 <= first <= last <= 65535",
                                  {"service": key, "range": [lo, hi]}))
                continue
            ranges[key] = (lo, hi)
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


def unknown_keys(d: dict) -> list[Result]:
    """Result(profile_key_unknown) for each key of the profile dict, or of its nested specs, that this
    version does not know; the detail is the (dotted) key."""
    found = [Result("profile_key_unknown", k, {"key": k})
             for k in sorted(d) if k not in {f.name for f in fields(HostProfile)}]
    nested = [("services", ServiceSpec), ("helpers", HelperSpec)]
    for group, spec in nested:
        known = {f.name for f in fields(spec)}
        for name, entry in sorted(d.get(group, {}).items() if isinstance(d.get(group), dict) else []):
            if isinstance(entry, dict):
                found += [Result("profile_key_unknown", f"{group}.{name}.{k}", {"key": f"{group}.{name}.{k}"})
                          for k in sorted(entry) if k not in known]
    override = d.get("port_override")
    if isinstance(override, dict):
        known = {f.name for f in fields(PortOverride)}
        found += [Result("profile_key_unknown", f"port_override.{k}", {"key": f"port_override.{k}"})
                  for k in sorted(override) if k not in known]
    return found


def _decode(cls, d: dict, where: str = ""):
    """Rebuild a dataclass from its encoded dict using the declared field types."""
    absent = [f.name for f in fields(cls) if f.name not in d and f.default is MISSING
              and f.default_factory is MISSING]
    if absent:
        names = ", ".join(f"{where}{n}" for n in absent)
        raise ProfileError(Result("profile_malformed", f"profile is missing key(s): {names}",
                                  {"missing": [f"{where}{n}" for n in absent]}))
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
            kw[name] = {k: _decode(ServiceSpec, x, f"services.{k}.") for k, x in v.items()}
        elif t.startswith("dict[str, HelperSpec"):
            kw[name] = {k: _decode(HelperSpec, x, f"helpers.{k}.") for k, x in v.items()}
        elif t == "PortOverride":
            kw[name] = _decode(_DATACLASSES[t], v, f"{name}.")
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
    if version < 1:
        return None, [Result("profile_malformed", f"{path}: schema_version {version}")]
    if version > SCHEMA_VERSION:
        return None, [Result("profile_schema_unsupported",
                             f"{path}: schema_version {version} > supported {SCHEMA_VERSION}",
                             {"found": version, "supported": SCHEMA_VERSION})]
    if "hook_paths" in d and not isinstance(d["hook_paths"], list):
        return None, [Result("profile_malformed", f"{path}: hook_paths must be a list")]
    try:
        profile = HostProfile.from_dict(d)
    except ProfileError as exc:
        return None, [Result(exc.result.code, f"{path}: {exc.result.detail}", exc.result.data)]
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return None, [Result("profile_malformed", f"{path}: {exc!r}")]
    return profile, unknown_keys(d)
