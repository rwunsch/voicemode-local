"""Shared result type (separate module to avoid backend<->package import cycles)."""
from dataclasses import dataclass


@dataclass
class Result:
    ok: bool
    state: str            # "on" | "off" | "status"-resolved | "unknown"
    detail: str = ""
    platform: str = ""
