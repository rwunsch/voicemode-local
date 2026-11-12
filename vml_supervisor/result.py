"""Outcome type shared by every supervisor operation."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Result:
    code: str
    detail: str = ""
    data: dict = field(default_factory=dict)


class Results(list):
    """A list collecting Result objects."""
