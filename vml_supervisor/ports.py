"""Port candidates and choice, driven by the HostProfile.

Moved from FELIX's port_candidates and voicemode_native's choose_port. The override is validated
as FELIX does (an int, not a bool, 1024-65535, not reserved); an invalid one is returned as a
Result next to the range candidates, never skipped silently.
"""
from __future__ import annotations

import json
import os

from .procs import port_free
from .profile import BASE_RESERVED_PORTS, HostProfile
from .result import Result


def _usable(port, reserved) -> bool:
    return (isinstance(port, int) and not isinstance(port, bool)
            and 1024 <= port <= 65535 and port not in reserved)


def _int_from_text(text):
    try:
        return int(str(text).strip())
    except ValueError:
        return None


def port_candidates(profile: HostProfile, kind: str) -> tuple[list[int], Result | None]:
    spec = profile.services.get(kind)
    if spec is None:
        return [], Result("unknown_service", f"the profile has no {kind!r} service", {"service": kind})
    lo, hi = spec.port_range
    base = list(range(lo, hi + 1))
    reserved = set(BASE_RESERVED_PORTS) | set(profile.reserved_ports)
    override = profile.port_override
    source = ""
    value = None
    if override.kind == "file":
        source = str(override.path)
        try:
            raw = json.loads(override.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return base, None
        except (OSError, ValueError) as error:
            return base, Result("port_override_invalid", f"{source} cannot be read as JSON: {error}",
                                {"source": source})
        if not isinstance(raw, dict):
            return base, Result("port_override_invalid", f"{source} must be a JSON object keyed by "
                                "service kind", {"source": source})
        if raw.get(kind) is None:
            return base, None
        value = raw[kind]
    elif override.kind == "env":
        # keys are positional by service kind: (tts, stt)
        index = {"tts": 0, "stt": 1}.get(kind)
        name = override.keys[index] if index is not None and index < len(override.keys) else None
        source = name or ""
        text = os.environ.get(name) if name else None
        if text is None or not text.strip():
            return base, None
        value = _int_from_text(text)
        if value is None:
            return base, Result("port_override_invalid", f"{name}={text!r} is not a number",
                                {"source": source, "value": text})
    else:
        return base, None
    if not _usable(value, reserved):
        return base, Result("port_override_invalid",
                            f"{source} sets {kind} to {value!r}; use a number from 1024 to 65535 "
                            f"other than {', '.join(map(str, sorted(reserved)))}",
                            {"source": source, "value": value})
    return [value, *[p for p in base if p != value]], None


def choose_port(profile: HostProfile, kind: str, taken: set[int]) -> int | None:
    """The first candidate not already given to another service and free on loopback."""
    candidates, _ = port_candidates(profile, kind)
    return next((p for p in candidates if p not in taken and port_free(p)), None)
