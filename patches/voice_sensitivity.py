"""How early voice mode ends a turn, changed live: VAD aggressiveness and silence length.

Upstream reads VOICEMODE_VAD_AGGRESSIVENESS and VOICEMODE_SILENCE_THRESHOLD_MS once, at
import (config.py:948-949), so a running voice server never sees a change to
voicemode.env. This module keeps an override in ``<base dir>/sensitivity.json``::

    {"vad_aggressiveness": 3, "silence_ms": 1200, "preset": "earlier"}

and patch_vad_override.py makes the recording function call ``resolve()`` at each
recording, so a change applies at the next listen: no restart, no /mcp reconnect.

Order, per field: an explicit converse ``vad_aggressiveness`` argument, then the override
file, then config. A field that is null, missing, the wrong type or out of range is no
override for that field; a missing or corrupt file is no override at all. With no file the
values are config's, exactly as before. Nothing here raises into the recording path.

The base dir is upstream's (VOICEMODE_BASE_DIR, else ~/.voicemode), read on every call.
Writes are atomic (temp file + os.replace in the same folder). Reads are cached by the
file's mtime and size; a file changed within the last second is not cached, because mtime
moves on a coarse clock tick.

Installed into voice_mode/voice_sensitivity.py by patches/apply.py. Stdlib only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("voicemode")

FILE_NAME = "sensitivity.json"
VAD_MIN, VAD_MAX = 0, 3
SILENCE_MIN_MS, SILENCE_MAX_MS = 300, 5000
# Upstream's defaults when its config cannot be read (config.py:948-949, 8.12.0).
UPSTREAM_VAD, UPSTREAM_SILENCE_MS = 3, 1000

# name -> (vad aggressiveness, silence ms, what it is for)
PRESETS = {
    "earlier": (3, 1200, "end the turn sooner: background noise counts as silence, 1.2 s of it ends the turn"),
    "noisy": (3, 800, "loud background: the strictest speech check and the shortest pause"),
    "later": (1, 2500, "wait longer: for pauses while thinking in a quiet room"),
}
ACTIONS = "status, set or reset"

_CACHE = {}          # path -> ((mtime_ns, size), parsed)
_FRESH_NS = 1_000_000_000


# -- where it lives -------------------------------------------------------------

def base_dir() -> Path:
    raw = os.environ.get("VOICEMODE_BASE_DIR")
    if raw:
        return Path(os.path.expanduser(os.path.expandvars(raw)))
    return Path.home() / ".voicemode"


def store_path() -> Path:
    return base_dir() / FILE_NAME


# -- validation -------------------------------------------------------------------

def _as_int(value):
    """An int from an int or a string of digits; None for anything else (bools and floats
    included: 2.5 is not a level, and a bool is not a number anyone meant)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _valid_vad(value):
    v = value if isinstance(value, int) and not isinstance(value, bool) else None
    return v if v is not None and VAD_MIN <= v <= VAD_MAX else None


def _valid_silence(value):
    v = value if isinstance(value, int) and not isinstance(value, bool) else None
    return v if v is not None and SILENCE_MIN_MS <= v <= SILENCE_MAX_MS else None


# -- reading ---------------------------------------------------------------------

def _parse(data: bytes):
    try:
        raw = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    vad = _valid_vad(raw.get("vad_aggressiveness"))
    silence = _valid_silence(raw.get("silence_ms"))
    if vad is None and silence is None:
        return None
    preset = raw.get("preset")
    return {"vad_aggressiveness": vad, "silence_ms": silence,
            "preset": preset if isinstance(preset, str) and preset in PRESETS else None}


def read_override():
    """The override as {"vad_aggressiveness", "silence_ms", "preset"}, or None. Never raises."""
    try:
        path = store_path()
        try:
            st = os.stat(path)
        except FileNotFoundError:
            _CACHE.pop(str(path), None)
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        hit = _CACHE.get(str(path))
        if hit is not None and hit[0] == stamp:
            return dict(hit[1]) if hit[1] is not None else None
        with open(path, "rb") as f:
            parsed = _parse(f.read(64 * 1024))
        if time.time_ns() - st.st_mtime_ns >= _FRESH_NS:
            _CACHE[str(path)] = (stamp, parsed)
        else:
            _CACHE.pop(str(path), None)
        return dict(parsed) if parsed is not None else None
    except Exception:
        return None


def resolve(explicit_vad, config_vad, config_silence_ms):
    """(vad aggressiveness, silence ms) for one recording: this call's argument, else the
    override, else config. Never raises; on any trouble the answer is config's."""
    vad = explicit_vad if explicit_vad is not None else config_vad
    silence = config_silence_ms
    try:
        o = read_override()
        if o:
            if explicit_vad is None and o["vad_aggressiveness"] is not None:
                vad = o["vad_aggressiveness"]
            if o["silence_ms"] is not None:
                silence = o["silence_ms"]
            logger.info(f"Voice sensitivity override ({FILE_NAME}): aggressiveness {vad}, "
                        f"silence {silence} ms")
    except Exception:
        return (explicit_vad if explicit_vad is not None else config_vad), config_silence_ms
    return vad, silence


# -- writing ---------------------------------------------------------------------

def _retrying(fn, *args):
    """On Windows a reader's open handle blocks os.replace/unlink for a moment: one retry."""
    try:
        return fn(*args)
    except PermissionError:
        if sys.platform != "win32":
            raise
        time.sleep(0.025)
        return fn(*args)


def write_override(vad, silence_ms, preset) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"vad_aggressiveness": vad, "silence_ms": silence_ms, "preset": preset})
    fd, tmp = tempfile.mkstemp(prefix=".sensitivity-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data + "\n")
        _retrying(os.replace, tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _CACHE.pop(str(path), None)


def clear_override() -> bool:
    """Remove the override; True if there was one. Idempotent."""
    path = store_path()
    _CACHE.pop(str(path), None)
    try:
        _retrying(os.unlink, str(path))
        return True
    except FileNotFoundError:
        return False


# -- the tool's words -----------------------------------------------------------

def configured_values(config=None):
    """(vad, silence ms) as upstream's config has them; else its environment, else its defaults."""
    if config is None:
        try:
            from voice_mode import config  # noqa: F811 -- only inside the voice server
        except Exception:
            config = None
    vad = getattr(config, "VAD_AGGRESSIVENESS", None)
    silence = getattr(config, "SILENCE_THRESHOLD_MS", None)
    if vad is None:
        vad = _as_int(os.environ.get("VOICEMODE_VAD_AGGRESSIVENESS", ""))
        vad = UPSTREAM_VAD if vad is None else vad
    if silence is None:
        silence = _as_int(os.environ.get("VOICEMODE_SILENCE_THRESHOLD_MS", ""))
        silence = UPSTREAM_SILENCE_MS if silence is None else silence
    return int(vad), int(silence)


def _pair(vad, silence) -> str:
    return f"aggressiveness {vad}, silence {silence} ms"


def _effective(configured):
    return resolve(None, configured[0], configured[1])


def _effective_line(configured) -> str:
    vad, silence = _effective(configured)
    return (f"In effect from the next listen: aggressiveness {vad} of 0-3 (higher counts more "
            f"background noise as silence), and the turn ends after {silence} ms of silence.")


def _status(configured) -> str:
    o = read_override()
    if o is None:
        override = "No override: the configured values apply."
    else:
        name = f"preset {o['preset']}: " if o["preset"] else ""
        parts = []
        if o["vad_aggressiveness"] is not None:
            parts.append(f"aggressiveness {o['vad_aggressiveness']}")
        if o["silence_ms"] is not None:
            parts.append(f"silence {o['silence_ms']} ms")
        override = f"Override ({FILE_NAME}): {name}{', '.join(parts)}."
    return "\n".join([f"Configured: {_pair(*configured)}.", override, _effective_line(configured)])


def _set(level, vad_in, silence_in, configured) -> str:
    refuse = "Not changed: "
    vad = silence = preset = None
    if level is not None:
        key = level.strip().lower() if isinstance(level, str) else None
        if key not in PRESETS:
            return (f"{refuse}unknown level {level!r}; use "
                    + ", ".join(PRESETS) + ", or numbers.")
        vad, silence, _ = PRESETS[key]
        preset = key
    if vad_in is not None:
        v = _valid_vad(_as_int(vad_in))
        if v is None:
            return f"{refuse}vad_aggressiveness must be a whole number from 0 to 3 (got {vad_in!r})."
        vad, preset = v, None
    if silence_in is not None:
        s = _valid_silence(_as_int(silence_in))
        if s is None:
            return f"{refuse}silence_ms must be a whole number from 300 to 5000 (got {silence_in!r})."
        silence, preset = s, None
    if level is None and vad_in is None and silence_in is None:
        return (f"Nothing to set ({refuse.lower().rstrip(': ')}): give a level ("
                + ", ".join(PRESETS) + ") or vad_aggressiveness (0-3) and/or silence_ms (300-5000).")
    if level is None:
        # Numbers alone change only what was given; the other field keeps the override's value.
        old = read_override() or {}
        if vad is None:
            vad = old.get("vad_aggressiveness")
        if silence is None:
            silence = old.get("silence_ms")
    try:
        write_override(vad, silence, preset)
    except Exception as e:
        return f"Could not save the override ({type(e).__name__}: {e}); nothing changed."
    eff = _effective(configured)
    named = f" (preset {preset})" if preset else ""
    return (f"Set{named}: {_pair(*eff)}. Applies from the next listen, no restart. "
            f"Say reset to go back to the configured {configured[0]} / {configured[1]} ms.")


def _reset(configured) -> str:
    try:
        removed = clear_override()
    except Exception as e:
        return f"Could not remove the override ({type(e).__name__}: {e}); nothing changed."
    head = "Override removed" if removed else "There was no override"
    return f"{head}: from the next listen {_pair(*_effective(configured))} (configured)."


def control(action="status", level=None, vad_aggressiveness=None, silence_ms=None, configured=None) -> str:
    """The voice_sensitivity tool's answer for one action. Never raises."""
    key = action.strip().lower() if isinstance(action, str) else None
    try:
        cfg = configured if configured is not None else configured_values()
        if key == "status":
            return _status(cfg)
        if key == "set":
            return _set(level, vad_aggressiveness, silence_ms, cfg)
        if key == "reset":
            return _reset(cfg)
    except Exception as e:
        return f"voice_sensitivity {key} failed ({type(e).__name__}: {e})."
    return f"Unknown action {action!r}: use {ACTIONS}."
