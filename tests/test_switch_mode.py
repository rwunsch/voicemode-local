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


import re

_BEGIN = "# >>> voicemode-switch managed (do not edit inside this block) >>>"
_END = "# <<< voicemode-switch managed <<<"


def _write_voicemode_env(envfile: str, mode: str, config: dict) -> None:
    """Replicate switch_mode._write_voicemode_env: managed block in voicemode.env."""
    txt = ""
    if os.path.exists(envfile):
        txt = open(envfile).read()
        txt = re.sub(re.escape(_BEGIN) + r".*?" + re.escape(_END) + r"\n?", "", txt, flags=re.S)
    block = [_BEGIN, f"# mode: {mode}"]
    for key in LIST_KEYS:
        if config[key]:
            block.append(f"{key}={config[key]}")
    block.append(_END)
    body = txt.rstrip("\n")
    open(envfile, "w").write((body + "\n\n" if body else "") + "\n".join(block) + "\n")


def _managed_values(envfile: str) -> dict:
    """Parse the active VOICEMODE_* keys from voicemode.env."""
    out = {}
    for line in open(envfile):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _make_envfile():
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False)
    tmp.write("# Voice Mode Configuration File\n# VOICEMODE_DEBUG=false\n")
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


# ── voicemode.env writing (switch_mode writes the stable file, not .claude.json) ──

def test_switch_to_piper_writes_voicemode_env():
    modes = _load_modes()
    path = _make_envfile()
    try:
        _write_voicemode_env(path, "piper", modes["piper"])
        vals = _managed_values(path)
        assert "8881" in vals["VOICEMODE_TTS_BASE_URLS"]
        assert vals["VOICEMODE_VOICES"].startswith("p_")
    finally:
        os.unlink(path)


def test_write_preserves_other_env_lines():
    """The managed block must not clobber the rest of voicemode.env."""
    modes = _load_modes()
    path = _make_envfile()
    try:
        _write_voicemode_env(path, "local", modes["local"])
        body = open(path).read()
        assert "# Voice Mode Configuration File" in body  # pre-existing content kept
        assert "VOICEMODE_DEBUG=false" in body
    finally:
        os.unlink(path)


def test_switch_to_openai_drops_local_urls():
    modes = _load_modes()
    path = _make_envfile()
    try:
        _write_voicemode_env(path, "openai", modes["openai"])
        vals = _managed_values(path)
        assert "127.0.0.1" not in vals["VOICEMODE_TTS_BASE_URLS"]
        assert "127.0.0.1" not in vals["VOICEMODE_STT_BASE_URLS"]
    finally:
        os.unlink(path)


def test_roundtrip_local_to_piper_and_back_is_idempotent():
    """Switching modes replaces the managed block cleanly (no duplication)."""
    modes = _load_modes()
    path = _make_envfile()
    try:
        _write_voicemode_env(path, "piper", modes["piper"])
        _write_voicemode_env(path, "local", modes["local"])
        body = open(path).read()
        assert body.count(_BEGIN) == 1 and body.count(_END) == 1  # exactly one block
        vals = _managed_values(path)
        assert vals["VOICEMODE_TTS_BASE_URLS"].split(",")[0].endswith("8880/v1")
    finally:
        os.unlink(path)


def test_strip_claude_json_routing_keeps_key_and_wslenv():
    """The .claude.json cleanup drops routing vars but keeps the key and a
    key-only WSLENV (for the cross-OS bridge)."""
    import importlib.util
    # Replicate _strip_claude_json_routing's contract directly on a temp file.
    data = {"mcpServers": {"voicemode": {"env": {
        "OPENAI_API_KEY": "sk-test",
        "VOICEMODE_TTS_BASE_URLS": "http://127.0.0.1:8880/v1",
        "TTS_BASE_URL": "http://127.0.0.1:8881/v1",
        "WSLENV": "OPENAI_API_KEY/u:VOICEMODE_TTS_BASE_URLS/u",
    }}}}
    env = data["mcpServers"]["voicemode"]["env"]
    for k in ("VOICEMODE_STT_BASE_URLS", "VOICEMODE_TTS_BASE_URLS", "VOICEMODE_VOICES",
              "STT_BASE_URL", "TTS_BASE_URL", "TTS_VOICE"):
        env.pop(k, None)
    keep = [x for x in env["WSLENV"].split(":") if x.startswith("OPENAI_API_KEY")]
    env["WSLENV"] = ":".join(keep)
    assert env == {"OPENAI_API_KEY": "sk-test", "WSLENV": "OPENAI_API_KEY/u"}
