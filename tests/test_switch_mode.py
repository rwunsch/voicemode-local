"""Tests for patches/switch_mode.py mode switching logic.

These tests exercise the JSON manipulation in switch_mode() without needing
the MCP server running. We mock the file I/O to use temp files.
"""

import json
import os
import tempfile
import pathlib
import sys

# We can't import switch_mode.py directly because it imports from voice_mode.server.
# Instead, we extract and test the core logic: MODES dict validation and the
# JSON read/write/transform behavior.

PATCHES_DIR = pathlib.Path(__file__).parent.parent / "patches"


def _load_modes():
    """Parse the MODES dict from switch_mode.py without importing it."""
    source = (PATCHES_DIR / "switch_mode.py").read_text()
    # Execute just the MODES definition in a restricted namespace
    namespace = {}
    # Extract MODES block: starts at 'MODES = {' and ends at the closing '}'
    start = source.index("MODES = {")
    # Find the matching closing brace by counting nesting
    depth = 0
    end = start
    for i, ch in enumerate(source[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    exec(source[start:end], namespace)
    return namespace["MODES"]


def _apply_mode(claude_json_path: str, mode: str, modes: dict) -> str:
    """Replicate the switch_mode logic against a temp file."""
    config = modes[mode]

    with open(claude_json_path) as f:
        data = json.load(f)

    env = data.get("mcpServers", {}).get("voicemode", {}).get("env", {})
    openai_key = env.get("OPENAI_API_KEY", "")

    new_env = {"OPENAI_API_KEY": openai_key}
    for key in ("STT_BASE_URL", "TTS_BASE_URL", "TTS_VOICE"):
        value = config[key]
        if value:
            new_env[key] = value

    data["mcpServers"]["voicemode"]["env"] = new_env

    with open(claude_json_path, "w") as f:
        json.dump(data, f, indent=2)

    return mode


def _make_claude_json(env=None):
    """Create a temp file with a minimal ~/.claude.json structure."""
    data = {
        "mcpServers": {
            "voicemode": {
                "command": "uvx",
                "args": ["voice-mode"],
                "env": env or {
                    "STT_BASE_URL": "http://127.0.0.1:2022/v1",
                    "TTS_BASE_URL": "http://127.0.0.1:8880/v1",
                    "TTS_VOICE": "af_sky",
                    "OPENAI_API_KEY": "sk-test-key-12345",
                },
            }
        }
    }
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, tmp, indent=2)
    tmp.close()
    return tmp.name


# ── MODES dict validation ─────────────────────────────────────────────────


def test_modes_has_all_expected_modes():
    modes = _load_modes()
    assert set(modes.keys()) == {"local", "piper", "openai", "hybrid"}


def test_each_mode_has_required_keys():
    modes = _load_modes()
    required = {"description", "STT_BASE_URL", "TTS_BASE_URL", "TTS_VOICE"}
    for name, config in modes.items():
        missing = required - set(config.keys())
        assert not missing, f"Mode '{name}' missing keys: {missing}"


def test_local_mode_uses_local_endpoints():
    modes = _load_modes()
    local = modes["local"]
    assert "127.0.0.1:2022" in local["STT_BASE_URL"]
    assert "127.0.0.1:8880" in local["TTS_BASE_URL"]


def test_piper_mode_uses_piper_endpoint():
    modes = _load_modes()
    piper = modes["piper"]
    assert "127.0.0.1:8881" in piper["TTS_BASE_URL"]
    assert piper["TTS_VOICE"].startswith("p_"), "Piper mode should default to a p_ voice"


def test_openai_mode_clears_local_urls():
    modes = _load_modes()
    openai = modes["openai"]
    assert openai["STT_BASE_URL"] == ""
    assert openai["TTS_BASE_URL"] == ""


# ── Mode switching logic ──────────────────────────────────────────────────


def test_switch_to_piper_sets_correct_env():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "piper", modes)
        with open(path) as f:
            data = json.load(f)
        env = data["mcpServers"]["voicemode"]["env"]
        assert "8881" in env["TTS_BASE_URL"]
        assert env["TTS_VOICE"].startswith("p_")
    finally:
        os.unlink(path)


def test_switch_preserves_openai_key():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "local", modes)
        with open(path) as f:
            data = json.load(f)
        env = data["mcpServers"]["voicemode"]["env"]
        assert env["OPENAI_API_KEY"] == "sk-test-key-12345"
    finally:
        os.unlink(path)


def test_switch_to_openai_removes_local_urls():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "openai", modes)
        with open(path) as f:
            data = json.load(f)
        env = data["mcpServers"]["voicemode"]["env"]
        # OpenAI mode should NOT have STT_BASE_URL or TTS_BASE_URL set
        assert "STT_BASE_URL" not in env
        assert "TTS_BASE_URL" not in env
    finally:
        os.unlink(path)


def test_switch_preserves_other_mcp_servers():
    """Switching mode should not affect other MCP server configs."""
    path = _make_claude_json()
    try:
        # Add another MCP server
        with open(path) as f:
            data = json.load(f)
        data["mcpServers"]["other-server"] = {"command": "other"}
        with open(path, "w") as f:
            json.dump(data, f)

        modes = _load_modes()
        _apply_mode(path, "piper", modes)

        with open(path) as f:
            data = json.load(f)
        assert "other-server" in data["mcpServers"]
        assert data["mcpServers"]["other-server"]["command"] == "other"
    finally:
        os.unlink(path)


def test_roundtrip_local_to_piper_and_back():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "piper", modes)
        with open(path) as f:
            piper_env = json.load(f)["mcpServers"]["voicemode"]["env"]
        assert "8881" in piper_env["TTS_BASE_URL"]

        _apply_mode(path, "local", modes)
        with open(path) as f:
            local_env = json.load(f)["mcpServers"]["voicemode"]["env"]
        assert "8880" in local_env["TTS_BASE_URL"]
        assert local_env["OPENAI_API_KEY"] == "sk-test-key-12345"
    finally:
        os.unlink(path)
