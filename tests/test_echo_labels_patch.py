"""patches/patch_echo_labels.py: the transcript echo lines in converse's tool description take
their labels from VOICEMODE_ECHO_USER_LABEL and VOICEMODE_ECHO_ASSISTANT_LABEL (FELIX sets the
user's name and "FELIX"); unset, the description is upstream's to the byte. Both docstrings
are relabelled: converse (the tool) and _converse_core, which repeats it."""
import importlib.util
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
PATCHER = PATCHES / "patch_echo_labels.py"

DOC = '''"""Have an ongoing voice conversation.

<echo>Transcript visibility: print `> **ASSISTANT (voicemode):** <message>` before calling, and `> **USER (voicemode):** <reply>` after a spoken reply.</echo>

       > **ASSISTANT (voicemode):** <message arg, verbatim>
       [voicemode:converse tool call]
       > **USER (voicemode):** <captured user message, verbatim>
   - ASSISTANT echo: always.
    """'''

CONVERSE = f'''\
REGISTERED = []


class _MCP:
    def tool(self):
        def register(fn):
            REGISTERED.append(fn.__doc__)
            return fn
        return register


mcp = _MCP()


async def _converse_core(
    message=None,
):
    {DOC}
    return "core"


@mcp.tool()
async def converse(
    message=None,
):
    {DOC}
    return await _converse_core(message=message)
'''


def _run(target):
    return subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True, text=True)


def _load(path):
    spec = importlib.util.spec_from_file_location("echo_fixture_" + uuid.uuid4().hex[:8], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def converse(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_ECHO_USER_LABEL", raising=False)
    monkeypatch.delenv("VOICEMODE_ECHO_ASSISTANT_LABEL", raising=False)
    target = tmp_path / "converse.py"
    target.write_text(CONVERSE)
    r = _run(target)
    assert r.returncode == 0, r.stdout + r.stderr
    return target


def test_labels_come_from_the_settings(converse, monkeypatch):
    monkeypatch.setenv("VOICEMODE_ECHO_USER_LABEL", "Stephan")
    monkeypatch.setenv("VOICEMODE_ECHO_ASSISTANT_LABEL", "FELIX")
    module = _load(converse)
    for doc in (module.REGISTERED[0], module._converse_core.__doc__, module.converse.__doc__):
        assert "> **FELIX (voicemode):** <message>" in doc
        assert "> **Stephan (voicemode):** <reply>" in doc
        assert doc.count("**FELIX (voicemode):**") == 2 and doc.count("**Stephan (voicemode):**") == 2
        assert "**ASSISTANT (voicemode):**" not in doc and "**USER (voicemode):**" not in doc


def test_unset_keeps_upstreams_text(converse):
    module = _load(converse)
    original = _load_unpatched(converse.parent)
    assert module.REGISTERED == original.REGISTERED
    assert module._converse_core.__doc__ == original._converse_core.__doc__


def test_one_label_alone_and_blank_is_unset(converse, monkeypatch):
    monkeypatch.setenv("VOICEMODE_ECHO_USER_LABEL", "  ")
    monkeypatch.setenv("VOICEMODE_ECHO_ASSISTANT_LABEL", "FELIX")
    doc = _load(converse).REGISTERED[0]
    assert "**FELIX (voicemode):**" in doc and "**USER (voicemode):**" in doc


def test_a_label_cannot_break_the_markdown(converse, monkeypatch):
    monkeypatch.setenv("VOICEMODE_ECHO_USER_LABEL", "**Ro\nbert**")
    doc = _load(converse).REGISTERED[0]
    assert "> **Robert (voicemode):** <reply>" in doc


def _load_unpatched(folder):
    plain = folder / "plain.py"
    plain.write_text(CONVERSE)
    return _load(plain)


def test_idempotent_and_drift(converse, tmp_path):
    once = converse.read_bytes()
    r = _run(converse)
    assert r.returncode == 0 and "already patched (echo labels)" in r.stdout
    assert converse.read_bytes() == once
    drift = tmp_path / "drift" / "converse.py"
    drift.parent.mkdir()
    drift.write_text(CONVERSE.replace("@mcp.tool()\n", "@mcp.tool(name='c')\n"))
    before = drift.read_bytes()
    r = _run(drift)
    assert r.returncode == 1 and "ANCHOR DRIFT" in r.stderr and drift.read_bytes() == before


def test_the_settings_pass_the_env_allowlist():
    """voicemode.env reaches os.environ only for VOICEMODE_* keys (patch_env_allowlist)."""
    for name in ("VOICEMODE_ECHO_USER_LABEL", "VOICEMODE_ECHO_ASSISTANT_LABEL"):
        assert name.startswith("VOICEMODE_")
        assert name in PATCHER.read_text(encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("VM812_SRC"), reason="VM812_SRC is not set")
def test_applies_to_pristine_8_12_0_both_docstrings(tmp_path):
    target = tmp_path / "converse.py"
    shutil.copy(Path(os.environ["VM812_SRC"]) / "converse.py", target)
    assert _run(target).returncode == 0
    text = target.read_text(encoding="utf-8")
    assert text.count("@_vml_echo_labels\n") == 2
    assert "@mcp.tool()\n@_vml_echo_labels\nasync def converse(\n" in text
    assert "@_vml_echo_labels\nasync def _converse_core(\n" in text
