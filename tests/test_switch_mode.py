"""Tests for patches/switch_mode.py mode switching logic.

These tests exercise the JSON manipulation in switch_mode() without needing
the MCP server running. We can't import switch_mode.py directly (it imports
from voice_mode.server), so we extract the MODES dict + replicate the env
transform. voice-mode only reads the *plural* VOICEMODE_*_BASE_URLS vars, so
that's what the modes must write.
"""

import json
import os
import tempfile
import pathlib

PATCHES_DIR = pathlib.Path(__file__).parent.parent / "patches"

LIST_KEYS = ("VOICEMODE_STT_BASE_URLS", "VOICEMODE_TTS_BASE_URLS", "VOICEMODE_VOICES")


def _load_modes():
    """Exec the endpoint constants + MODES dict from switch_mode.py."""
    source = (PATCHES_DIR / "switch_mode.py").read_text()
    namespace = {}
    start = source.index("KOKORO =")
    # find end of the MODES dict (matching brace from 'MODES = {')
    mstart = source.index("MODES = {")
    depth, end = 0, mstart
    for i, ch in enumerate(source[mstart:], mstart):
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
    """Replicate switch_mode()'s env transform against a temp file."""
    config = modes[mode]
    with open(claude_json_path) as f:
        data = json.load(f)
    env = data.get("mcpServers", {}).get("voicemode", {}).get("env", {})
    new_env = {"OPENAI_API_KEY": env.get("OPENAI_API_KEY", "")}
    if env.get("WSLENV"):
        new_env["WSLENV"] = env["WSLENV"]
    for key in LIST_KEYS:
        if config[key]:
            new_env[key] = config[key]
    data["mcpServers"]["voicemode"]["env"] = new_env
    with open(claude_json_path, "w") as f:
        json.dump(data, f, indent=2)
    return mode


def _make_claude_json(env=None):
    data = {
        "mcpServers": {
            "voicemode": {
                "command": "uvx",
                "args": ["voice-mode"],
                "env": env or {
                    "VOICEMODE_STT_BASE_URLS": "http://127.0.0.1:2022/v1",
                    "VOICEMODE_TTS_BASE_URLS": "http://127.0.0.1:8880/v1",
                    "VOICEMODE_VOICES": "af_sky",
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
    assert set(modes.keys()) == {"local", "localonly", "piper", "openai", "hybrid"}


def test_each_mode_has_required_keys():
    modes = _load_modes()
    required = {"description", *LIST_KEYS}
    for name, config in modes.items():
        missing = required - set(config.keys())
        assert not missing, f"Mode '{name}' missing keys: {missing}"


def test_modes_write_plural_vars_not_dead_singular():
    """Regression: the singular TTS_BASE_URL/STT_BASE_URL are NOT read by
    voice-mode; modes must write the plural list vars."""
    modes = _load_modes()
    for name, config in modes.items():
        assert "TTS_BASE_URL" not in config
        assert "STT_BASE_URL" not in config


def test_local_mode_has_kokoro_and_piper_then_openai_last():
    modes = _load_modes()
    tts = modes["local"]["VOICEMODE_TTS_BASE_URLS"]
    assert "8880" in tts and "8881" in tts
    # OpenAI strictly last
    assert tts.rstrip("/").endswith("openai.com/v1") or "openai.com" in tts.split(",")[-1]
    assert "8880" in modes["local"]["VOICEMODE_STT_BASE_URLS"] or "2022" in modes["local"]["VOICEMODE_STT_BASE_URLS"]


def test_localonly_mode_has_no_cloud():
    modes = _load_modes()
    assert "openai.com" not in modes["localonly"]["VOICEMODE_TTS_BASE_URLS"]
    assert "openai.com" not in modes["localonly"]["VOICEMODE_STT_BASE_URLS"]


def test_piper_mode_is_piper_primary():
    modes = _load_modes()
    tts = modes["piper"]["VOICEMODE_TTS_BASE_URLS"]
    assert tts.split(",")[0].endswith("8881/v1"), "Piper should be first endpoint"
    assert modes["piper"]["VOICEMODE_VOICES"].startswith("p_")


def test_openai_mode_is_cloud_only():
    modes = _load_modes()
    assert modes["openai"]["VOICEMODE_TTS_BASE_URLS"] == "https://api.openai.com/v1"
    assert modes["openai"]["VOICEMODE_STT_BASE_URLS"] == "https://api.openai.com/v1"


# ── Mode switching logic ──────────────────────────────────────────────────

def test_switch_to_piper_sets_correct_env():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "piper", modes)
        env = json.load(open(path))["mcpServers"]["voicemode"]["env"]
        assert "8881" in env["VOICEMODE_TTS_BASE_URLS"]
        assert env["VOICEMODE_VOICES"].startswith("p_")
    finally:
        os.unlink(path)


def test_switch_preserves_openai_key():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "local", modes)
        env = json.load(open(path))["mcpServers"]["voicemode"]["env"]
        assert env["OPENAI_API_KEY"] == "sk-test-key-12345"
    finally:
        os.unlink(path)


def test_switch_preserves_wslenv_bridge():
    """Cross-OS bridge: a WSLENV passthrough must survive a mode switch."""
    modes = _load_modes()
    path = _make_claude_json(env={
        "OPENAI_API_KEY": "sk-test",
        "VOICEMODE_TTS_BASE_URLS": "http://127.0.0.1:8880/v1",
        "WSLENV": "OPENAI_API_KEY/u:VOICEMODE_TTS_BASE_URLS/u",
    })
    try:
        _apply_mode(path, "piper", modes)
        env = json.load(open(path))["mcpServers"]["voicemode"]["env"]
        assert env["WSLENV"] == "OPENAI_API_KEY/u:VOICEMODE_TTS_BASE_URLS/u"
    finally:
        os.unlink(path)


def test_switch_to_openai_drops_local_urls():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "openai", modes)
        env = json.load(open(path))["mcpServers"]["voicemode"]["env"]
        assert "127.0.0.1" not in env["VOICEMODE_TTS_BASE_URLS"]
        assert "127.0.0.1" not in env["VOICEMODE_STT_BASE_URLS"]
    finally:
        os.unlink(path)


def test_switch_preserves_other_mcp_servers():
    path = _make_claude_json()
    try:
        data = json.load(open(path))
        data["mcpServers"]["other-server"] = {"command": "other"}
        json.dump(data, open(path, "w"))
        _apply_mode(path, "piper", _load_modes())
        data = json.load(open(path))
        assert data["mcpServers"]["other-server"]["command"] == "other"
    finally:
        os.unlink(path)


def test_roundtrip_local_to_piper_and_back():
    modes = _load_modes()
    path = _make_claude_json()
    try:
        _apply_mode(path, "piper", modes)
        piper_env = json.load(open(path))["mcpServers"]["voicemode"]["env"]
        assert piper_env["VOICEMODE_TTS_BASE_URLS"].split(",")[0].endswith("8881/v1")
        _apply_mode(path, "local", modes)
        local_env = json.load(open(path))["mcpServers"]["voicemode"]["env"]
        assert local_env["VOICEMODE_TTS_BASE_URLS"].split(",")[0].endswith("8880/v1")
        assert local_env["OPENAI_API_KEY"] == "sk-test-key-12345"
    finally:
        os.unlink(path)
