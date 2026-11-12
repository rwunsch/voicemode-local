"""Only voice settings travel from voicemode.env into the server's environment
(patches/patch_env_allowlist.py).

Final review 2026-11-12 (C1): upstream's update_config MCP tool writes any key into
voicemode.env and load_voicemode_env copies every key into os.environ, which every child
process inherits. BASH_ENV=$(cmd) then runs cmd when the server starts voicemode-switch
(bash). Refusing named settings cannot close that; an allowlist does.

Run with: VM812_SRC=<dir holding pristine 8.12.0 sources> pytest tests/test_env_allowlist.py
"""
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
SCRIPT = PATCHES / "patch_env_allowlist.py"


@pytest.fixture
def pristine(tmp_path):
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC not set (dir holding pristine 8.12.0 sources)")
    out = {}
    for name in ("config.py", "configuration_management.py"):
        shutil.copyfile(Path(raw) / name, tmp_path / name)
        out[name] = tmp_path / name
    return out


def _apply(target):
    return subprocess.run([sys.executable, str(SCRIPT), str(target)], capture_output=True, text=True)


def _function(source, name):
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return ast.get_source_segment(source, node)


@pytest.mark.parametrize("name", ["config.py", "configuration_management.py"])
def test_the_patch_applies_once_and_compiles(pristine, name):
    target = pristine[name]
    assert _apply(target).returncode == 0
    text = target.read_text()
    compile(text, name, "exec")
    assert "VML: ENV ALLOWLIST" in text
    again = _apply(target)
    assert again.returncode == 0 and target.read_text() == text


@pytest.mark.parametrize("key,allowed", [
    ("VOICEMODE_VOICES", True), ("VOICEMODE_PILL", True),
    # Provider keys only with the optional patch_env_provider_keys.py (FELIX never sets one).
    ("OPENAI_API_KEY", False), ("CARTESIA_API_KEY", False), ("VOICE_MODE_AUTO_START_KOKORO", True),
    ("BASH_ENV", False), ("LD_PRELOAD", False), ("PATH", False), ("PYTHONSTARTUP", False),
    ("BROWSER", False), ("NODE_OPTIONS", False), ("VOICEMODEX", False),
])
def test_only_voice_keys_are_allowed(pristine, key, allowed):
    assert _apply(pristine["config.py"]).returncode == 0
    ns = {}
    src = pristine["config.py"].read_text()
    exec(_function(src, "_vml_env_key_allowed"), _globals(src, ns))
    assert ns["_vml_env_key_allowed"](key) is allowed


def test_the_loader_copies_only_allowed_keys(pristine, tmp_path, monkeypatch):
    assert _apply(pristine["config.py"]).returncode == 0
    src = pristine["config.py"].read_text()
    env_file = tmp_path / "voicemode.env"
    env_file.write_text("BASH_ENV=$(touch /tmp/pwned)\nVOICEMODE_X_TEST=1\nOPENAI_API_KEY=sk-test\nPATH=/evil\n")
    for k in ("BASH_ENV", "VOICEMODE_X_TEST", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    ns = {"os": os, "Path": Path, "find_voicemode_env_files": lambda: [env_file]}
    exec(_function(src, "_vml_env_key_allowed"), _globals(src, ns))
    exec(_function(src, "load_voicemode_env"), ns)
    path_before = os.environ.get("PATH")
    ns["load_voicemode_env"]()
    assert "BASH_ENV" not in os.environ
    assert os.environ.get("VOICEMODE_X_TEST") == "1"
    assert "OPENAI_API_KEY" not in os.environ          # only with the provider-keys patch
    assert os.environ.get("PATH") == path_before


def test_update_config_refuses_a_key_that_is_not_a_voice_setting(pristine):
    assert _apply(pristine["configuration_management.py"]).returncode == 0
    body = _function(pristine["configuration_management.py"].read_text(), "update_config")
    refuse = body.index("_vml_env_key_allowed(key)")
    assert refuse < body.index("config_path = USER_CONFIG_PATH")   # before anything is written


def test_drift_fails_and_writes_nothing(pristine):
    target = pristine["config.py"]
    text = target.read_text().replace("                            os.environ[key] = value\n", "")
    target.write_text(text)
    assert _apply(target).returncode != 0
    assert target.read_text() == text


PROVIDER = PATCHES / "patch_env_provider_keys.py"
PROVIDER_KEYS = ("OPENAI" + "_API_KEY", "CARTESIA" + "_API_KEY")


def test_the_core_patch_names_no_provider_key():
    # FELIX's repo test (Robert, 2026-11-03: FELIX never uses or sets a model provider's
    # API key) scans every vendored file for these names.
    text = SCRIPT.read_text()
    assert not any(k in text for k in PROVIDER_KEYS)


@pytest.mark.parametrize("name", ["config.py", "configuration_management.py"])
def test_the_provider_keys_patch_adds_the_two_keys(pristine, name):
    assert _apply(pristine[name]).returncode == 0
    done = subprocess.run([sys.executable, str(PROVIDER), str(pristine[name])], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    text = pristine[name].read_text()
    compile(text, name, "exec")
    ns = {}
    exec(_function(text, "_vml_env_key_allowed"), _globals(text, ns))
    assert all(ns["_vml_env_key_allowed"](k) for k in PROVIDER_KEYS)
    assert not ns["_vml_env_key_allowed"]("BASH_ENV")
    again = subprocess.run([sys.executable, str(PROVIDER), str(pristine[name])], capture_output=True, text=True)
    assert again.returncode == 0 and pristine[name].read_text() == text


def test_the_provider_keys_patch_needs_the_core_patch_first(pristine):
    done = subprocess.run([sys.executable, str(PROVIDER), str(pristine["config.py"])], capture_output=True, text=True)
    assert done.returncode != 0


def _globals(text, ns):
    """The module-level constants the helper reads (the extra-key tuple)."""
    tree = ast.parse(text)
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "").startswith("_VML_") for t in n.targets):
            exec(ast.get_source_segment(text, n), ns)
    return ns


def test_update_config_keeps_its_mcp_decorator(pristine):
    # The helper was first inserted between @mcp.tool() and update_config: the decorator
    # then registered the helper as an MCP tool, and update_config was no tool at all.
    assert _apply(pristine["configuration_management.py"]).returncode == 0
    tree = ast.parse(pristine["configuration_management.py"].read_text())
    fns = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert fns["update_config"].decorator_list, "update_config lost @mcp.tool()"
    assert not fns["_vml_env_key_allowed"].decorator_list, "the helper became an MCP tool"
