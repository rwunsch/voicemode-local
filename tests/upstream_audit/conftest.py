import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def vm812_dir() -> Path:
    """The clean voice-mode 8.12.0 package built by Phase A Task 1."""
    raw = os.environ.get("VM812_DIR")
    if not raw:
        pytest.skip("VM812_DIR not set - run Task 1 first")
    p = Path(raw)
    if not (p / "tools" / "converse.py").exists():
        pytest.fail(f"VM812_DIR={p} does not look like a voice_mode package")
    return p


@pytest.fixture(scope="session")
def converse_src(vm812_dir: Path) -> str:
    return (vm812_dir / "tools" / "converse.py").read_text()
