"""voicemode-switch env writes: keep the user's file, voice and messages.

Runs the real script with HOME in a temp dir, so the user's ~/.voicemode and
~/.claude.json are never touched. A mode switch changes routing, not the voice.
"""
import json
import os
import subprocess
from pathlib import Path

SWITCH = Path(__file__).resolve().parent.parent / "voicemode-switch"
BEGIN = "# >>> voicemode-switch managed (do not edit inside this block) >>>"
END = "# <<< voicemode-switch managed <<<"


def run(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run([str(SWITCH), *args], env=env, capture_output=True, text=True, timeout=30)


def envfile(home: Path) -> Path:
    return home / ".voicemode" / "voicemode.env"


def set_env_value(home: Path, key: str, value: str) -> None:
    src = SWITCH.read_text().split("set_env_value() {", 1)[1].split("\n}\n", 1)[0]
    script = "set_env_value() {" + src + "\n}\n" + f'set_env_value "{key}" "{value}"\n'
    subprocess.run(["bash", "-c", script], env={**os.environ, "HOME": str(home)}, check=True, timeout=30)


def block(mode="localonly", voices="af_sky", stt="http://127.0.0.1:2022/v1",
          tts="http://127.0.0.1:8880/v1,http://127.0.0.1:8881/v1"):
    lines = [BEGIN, f"# mode: {mode}", f"VOICEMODE_STT_BASE_URLS={stt}", f"VOICEMODE_TTS_BASE_URLS={tts}"]
    if voices:
        lines.append(f"VOICEMODE_VOICES={voices}")
    return "\n".join(lines + [END]) + "\n"


# -- line order and comments -------------------------------------------------

def test_a_mode_switch_keeps_the_block_where_it_is_and_every_other_line(tmp_path):
    # Fails if the managed block is removed and re-appended at the end (the line order changes).
    envfile(tmp_path).parent.mkdir()
    before = "# my notes\nA=1\n" + block(mode="hybrid") + "# mid comment\nB=2\n\nC=3\n"
    envfile(tmp_path).write_text(before)
    assert run(tmp_path, "localonly").returncode == 0
    after = envfile(tmp_path).read_text()
    assert after.index("# my notes") < after.index("A=1") < after.index(BEGIN) < after.index(END) < after.index("# mid comment") < after.index("B=2") < after.index("C=3")
    assert "# mode: localonly" in after and "# mode: hybrid" not in after
    assert after.count(BEGIN) == 1


def test_a_mode_switch_without_a_block_appends_one_at_the_end(tmp_path):
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("A=1\n# note\nB=2\n")
    run(tmp_path, "localonly")
    after = envfile(tmp_path).read_text()
    assert after.startswith("A=1\n# note\nB=2\n")
    assert after.index("B=2") < after.index(BEGIN)


def test_the_same_mode_twice_leaves_the_file_byte_identical(tmp_path):
    # Fails if a re-run rewrites or reorders the file.
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("A=1\n" + block(mode="hybrid") + "B=2\n")
    run(tmp_path, "localonly")
    once = envfile(tmp_path).read_bytes()
    run(tmp_path, "localonly")
    assert envfile(tmp_path).read_bytes() == once


def test_set_env_value_replaces_in_place_and_appends_new_keys_at_the_end(tmp_path):
    # Fails if set_env_value deletes the key and appends it (moves the line, drops nothing else).
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("# top\nVOICEMODE_PILL=false\nOTHER=x\n# bottom\n")
    set_env_value(tmp_path, "VOICEMODE_PILL", "true")
    assert envfile(tmp_path).read_text() == "# top\nVOICEMODE_PILL=true\nOTHER=x\n# bottom\n"
    set_env_value(tmp_path, "NEW_KEY", "1")
    assert envfile(tmp_path).read_text() == "# top\nVOICEMODE_PILL=true\nOTHER=x\n# bottom\nNEW_KEY=1\n"


def test_set_env_value_on_a_missing_file_creates_it(tmp_path):
    set_env_value(tmp_path, "K", "v")
    assert envfile(tmp_path).read_text() == "K=v\n"


# -- the voice ---------------------------------------------------------------

def test_a_mode_switch_keeps_the_users_default_voice(tmp_path):
    # Fails if the mode's own voice (af_sky) overwrites a chosen voice such as am_puck.
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text(block(mode="hybrid", voices="am_puck"))
    run(tmp_path, "localonly")
    text = envfile(tmp_path).read_text()
    assert "VOICEMODE_VOICES=am_puck\n" in text and "af_sky" not in text
    run(tmp_path, "piper")
    assert "VOICEMODE_VOICES=am_puck\n" in envfile(tmp_path).read_text()


def test_a_voice_outside_the_block_is_kept_and_not_duplicated(tmp_path):
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("VOICEMODE_VOICES=am_puck\n" + block(mode="hybrid", voices=""))
    run(tmp_path, "localonly")
    text = envfile(tmp_path).read_text()
    assert text.count("VOICEMODE_VOICES=") == 1 and text.startswith("VOICEMODE_VOICES=am_puck\n")


def test_the_voice_is_set_when_absent(tmp_path):
    run(tmp_path, "localonly")
    assert "VOICEMODE_VOICES=af_sky\n" in envfile(tmp_path).read_text()


def test_a_mode_default_left_by_an_earlier_switch_follows_the_new_mode(tmp_path):
    # An earlier switch wrote af_sky itself; going to cloud-only must not leave a voice OpenAI rejects.
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text(block(mode="localonly", voices="af_sky"))
    run(tmp_path, "openai")
    assert "VOICEMODE_VOICES=nova\n" in envfile(tmp_path).read_text()


# -- ~/.claude.json message --------------------------------------------------

def test_claude_json_message_only_when_keys_were_removed(tmp_path):
    # Fails if "Cleaned routing vars" prints (and the file is rewritten) when there was nothing to clean.
    cj = tmp_path / ".claude.json"
    clean = json.dumps({"mcpServers": {"voicemode": {"env": {"OPENAI_API_KEY": "k"}}}})
    cj.write_text(clean)
    done = run(tmp_path, "localonly")
    assert "Cleaned routing vars" not in done.stdout
    assert cj.read_text() == clean


def test_claude_json_with_routing_keys_is_cleaned_and_says_so(tmp_path):
    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"mcpServers": {"voicemode": {"env": {
        "OPENAI_API_KEY": "k", "VOICEMODE_VOICES": "af_sky", "TTS_BASE_URL": "x"}}}}))
    done = run(tmp_path, "localonly")
    assert "Cleaned routing vars" in done.stdout
    env = json.loads(cj.read_text())["mcpServers"]["voicemode"]["env"]
    assert env == {"OPENAI_API_KEY": "k"}
