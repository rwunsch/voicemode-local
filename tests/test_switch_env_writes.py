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
    script = f'source "{SWITCH}"\nset_env_value "{key}" "{value}"\n'
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


# -- the settings lock shared with the native switch and the pill ---------------
#
# voicemode_native.settings_lock takes flock(2) on voicemode.env.lock. Each shell writer
# must take the same lock, or a read-modify-write by one loses the other's change.

import sys
import time

NATIVE = SWITCH.parent / "scripts" / "windows" / "voicemode_native.py"

# Holds the native switch's own lock, then does a slow read-modify-write under it.
HOLDER = r'''
import importlib.util, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("vn_lock_holder", sys.argv[1])
vn = importlib.util.module_from_spec(spec); spec.loader.exec_module(vn)
env, ready, hold = Path(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4])
with vn.settings_lock(env):
    text = env.read_text() if env.exists() else ""
    ready.write_text("locked")
    time.sleep(hold)
    env.write_text(text + "HELD_BY_NATIVE=1\n")
'''


def sourced(home: Path, body: str, extra_env=None) -> subprocess.CompletedProcess:
    """Run shell code with voicemode-switch's functions defined and nothing dispatched."""
    env = {**os.environ, "HOME": str(home), **(extra_env or {})}
    return subprocess.run(["bash", "-c", f'source "{SWITCH}"\n{body}'], env=env,
                          capture_output=True, text=True, timeout=60)


def hold_native_lock(home: Path, hold: float):
    envfile(home).parent.mkdir(exist_ok=True)
    ready = home / "ready"
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, str(NATIVE), str(envfile(home)),
                             str(ready), str(hold)])
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert proc.poll() is None, "lock holder died"
        assert time.monotonic() < deadline, "lock holder never took the lock"
        time.sleep(0.02)
    return proc


WRITERS = {
    "set_env_value": ('set_env_value SHELL_KEY 1', "SHELL_KEY=1"),
    "set_voice_barge": ("set_voice_barge true", "VOICEMODE_VOICE_BARGE=true"),
    "update_env": ('update_env "http://127.0.0.1:2022/v1" "http://127.0.0.1:8880/v1" af_sky localonly'
                   ' >/dev/null', "# mode: localonly"),
    "expose_barge_tool": ("expose_barge_tool", "VOICEMODE_TOOLS_ENABLED=converse,voice_barge_tool"),
}


def test_sourcing_the_switch_defines_its_functions_and_runs_nothing(tmp_path):
    r = sourced(tmp_path, "type set_env_value >/dev/null && echo defined")
    assert r.returncode == 0 and r.stdout.strip() == "defined", r.stderr
    assert not (tmp_path / ".voicemode").exists()


import pytest  # noqa: E402


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_a_shell_write_waits_for_the_native_switch_and_keeps_its_change(tmp_path, name):
    call, expect = WRITERS[name]
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("VOICEMODE_TOOLS_ENABLED=converse\n")
    holder = hold_native_lock(tmp_path, 1.0)
    try:
        r = sourced(tmp_path, call)
    finally:
        assert holder.wait(timeout=20) == 0
    assert r.returncode == 0, r.stderr
    text = envfile(tmp_path).read_text()
    assert "HELD_BY_NATIVE=1" in text, f"{name} lost the native switch's write:\n{text}"
    assert expect in text, text


def test_audio_token_waits_for_the_native_switch(tmp_path):
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("A=1\n")
    holder = hold_native_lock(tmp_path, 1.0)
    try:
        r = run(tmp_path, "audio-token", "on")
    finally:
        assert holder.wait(timeout=20) == 0
    assert r.returncode == 0, r.stderr
    text = envfile(tmp_path).read_text()
    assert "HELD_BY_NATIVE=1" in text and "VOICEMODE_AUDIO_TOKEN_ENABLED=true" in text, text


def test_two_shell_writers_at_once_lose_nothing(tmp_path):
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("")
    loop = 'for i in $(seq 1 15); do set_env_value {p}_$i $i; done'
    env = {**os.environ, "HOME": str(tmp_path)}
    procs = [subprocess.Popen(["bash", "-c", f'source "{SWITCH}"\n' + loop.format(p=p)], env=env)
             for p in ("LEFT", "RIGHT")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    text = envfile(tmp_path).read_text()
    missing = [f"{p}_{i}" for p in ("LEFT", "RIGHT") for i in range(1, 16) if f"{p}_{i}={i}" not in text]
    assert missing == [], text


def test_a_lock_held_too_long_fails_the_write_and_leaves_the_file(tmp_path):
    envfile(tmp_path).parent.mkdir()
    envfile(tmp_path).write_text("A=1\n")
    holder = hold_native_lock(tmp_path, 3.0)
    try:
        r = sourced(tmp_path, "set_env_value SHELL_KEY 1", {"VOICEMODE_ENV_LOCK_TIMEOUT": "0.5"})
    finally:
        holder.wait(timeout=20)
    assert r.returncode != 0
    assert "voicemode.env.lock" in r.stdout + r.stderr
    assert "SHELL_KEY" not in envfile(tmp_path).read_text()


def test_the_lock_is_released_after_each_write(tmp_path):
    r = sourced(tmp_path, "set_env_value A 1 && set_voice_barge true && set_env_value B 2")
    assert r.returncode == 0, r.stderr
    holder = hold_native_lock(tmp_path, 0.1)   # would hang if the shell still held it
    assert holder.wait(timeout=20) == 0
