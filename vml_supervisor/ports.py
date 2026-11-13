"""Port candidates and choice, driven by the HostProfile.

Moved from FELIX's port_candidates and voicemode_native's choose_port. The override is validated
as FELIX does (an int, not a bool, 1024-65535, not reserved); an invalid one is returned as a
Result next to the range candidates, never skipped silently.
"""
from __future__ import annotations

import json
import os

from .procs import port_free
from .profile import BASE_RESERVED_PORTS, SERVICE_KINDS, HostProfile
from .result import Result


def _usable(port, reserved) -> bool:
    return (isinstance(port, int) and not isinstance(port, bool)
            and 1024 <= port <= 65535 and port not in reserved)


def _int_from_text(text):
    try:
        return int(str(text).strip())
    except ValueError:
        return None


def _env_name(keys, kind: str):
    """The environment variable for `kind`: an entry "kind=NAME" names its own kind; a plain entry
    belongs to the kind at its position in SERVICE_KINDS (the order the profile format documents)."""
    for position, key in enumerate(keys):
        named_kind, separator, name = key.partition("=")
        if separator:
            if named_kind == kind:
                return name or None
        elif position < len(SERVICE_KINDS) and SERVICE_KINDS[position] == kind:
            return key
    return None


def _override(profile: HostProfile, kind: str):
    """(value, source, problem): the override's port for this kind when one is set and usable,
    else (None, source, the Result saying why it is unusable, or None when none is set)."""
    reserved = set(BASE_RESERVED_PORTS) | set(profile.reserved_ports)
    override = profile.port_override
    source = ""
    value = None
    if override.kind == "file":
        source = str(override.path)
        try:
            raw = json.loads(override.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None, source, None
        except (OSError, ValueError) as error:
            return None, source, Result("port_override_invalid", f"{source} cannot be read as JSON: {error}",
                                        {"source": source})
        if not isinstance(raw, dict):
            return None, source, Result("port_override_invalid", f"{source} must be a JSON object keyed by "
                                        "service kind", {"source": source})
        if raw.get(kind) is None:
            return None, source, None
        value = raw[kind]
    elif override.kind == "env":
        name = _env_name(override.keys, kind)
        source = name or ""
        text = os.environ.get(name) if name else None
        if text is None or not text.strip():
            return None, source, None
        value = _int_from_text(text)
        if value is None:
            return None, source, Result("port_override_invalid", f"{name}={text!r} is not a number",
                                        {"source": source, "value": text})
    else:
        return None, source, None
    if not _usable(value, reserved):
        return None, source, Result("port_override_invalid",
                                    f"{source} sets {kind} to {value!r}; use a number from 1024 to 65535 "
                                    f"other than {', '.join(map(str, sorted(reserved)))}",
                                    {"source": source, "value": value})
    return value, source, None


def requested_port(profile: HostProfile, kind: str) -> tuple[int | None, str]:
    """(the port the override asks for, its source), or (None, source) when none usable is set."""
    value, source, _ = _override(profile, kind)
    return value, source


def port_candidates(profile: HostProfile, kind: str) -> tuple[list[int], Result | None]:
    spec = profile.services.get(kind)
    if spec is None:
        return [], Result("unknown_service", f"the profile has no {kind!r} service", {"service": kind})
    lo, hi = spec.port_range
    base = list(range(lo, hi + 1))
    value, _, problem = _override(profile, kind)
    if value is None:
        return base, problem
    return [value, *[p for p in base if p != value]], None


def choose_port(profile: HostProfile, kind: str, taken: set[int]) -> int | None:
    """The first candidate not already given to another service and free on loopback."""
    candidates, _ = port_candidates(profile, kind)
    return next((p for p in candidates if p not in taken and port_free(p)), None)
