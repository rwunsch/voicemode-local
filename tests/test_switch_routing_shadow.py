"""voicemode-switch: an active line outside the managed block must not shadow it.

voice_mode.config reads voicemode.env top to bottom and keeps the FIRST assignment of a key
(`if key not in os.environ`). Voice-mode's own template, or an older tool, can leave an active
`VOICEMODE_STT_BASE_URLS=...` above the block; the block's value is then never used. Found
2026-11-17: the block said 2022,2023 and voice-mode routed to 2022 only.

Runs the real script with HOME in a temp dir; the user's ~/.voicemode is never touched.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SWITCH = Path(__file__).resolve().parent.parent / "voicemode-switch"
BEGIN = "# >>> voicemode-switch managed (do not edit inside this block) >>>"
END = "# <<< voicemode-switch managed <<<"
MARK = "# superseded by voicemode-switch managed block: "

S2022 = "http://127.0.0.1:2022/v1"
S2023 = "http://127.0.0.1:2023/v1"
K8880 = "http://127.0.0.1:8880/v1"
P8881 = "http://127.0.0.1:8881/v1"

# Shaped like voice-mode's template: comments, commented defaults, active lines, a multiline value.
TEMPLATE = (
    "# Voice Mode Configuration File\n"
    "# VOICEMODE_STT_BASE_URLS=http://127.0.0.1:2022/v1,https://api.openai.com/v1\n"
    "VOICEMODE_DEBUG=true\n"
    f"VOICEMODE_TTS_BASE_URLS={K8880}\n"
    f"VOICEMODE_STT_BASE_URLS={S2022}\n"
    "VOICEMODE_VOICES=am_puck\n"
    "VOICEMODE_PRONOUNCE='\n"
    "TTS \\bfoo\\b bar\n"
    "'\n"
    "export VOICEMODE_SAVE_ALL=false\n"
)


def config_python() -> str:
    """A python that imports voice_mode.config (the repo .venv runs these tests), or skip."""
    import tempfile
    py = os.environ.get("VOICEMODE_TEST_CONFIG_PY", sys.executable)
    with tempfile.TemporaryDirectory() as home:   # the import writes a default voicemode.env
        ok = subprocess.run([py, "-c", "import voice_mode.config"], capture_output=True,
                            env={**os.environ, "HOME": home}, timeout=60)
    if ok.returncode != 0:
        pytest.skip(f"voice_mode not importable with {py}")
    return py


def envfile(home: Path) -> Path:
    return home / ".voicemode" / "voicemode.env"


def clean_env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VOICEMODE_")}
    return {**env, "HOME": str(home), **extra}


def sourced(home: Path, body: str, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'source "{SWITCH}"\n{body}'], env=clean_env(home, **extra),
                          capture_output=True, text=True, timeout=120)


def update_env(home: Path, stt: str, tts: str, voices="af_sky", mode="localonly", timeout=""):
    r = sourced(home, f'update_env "{stt}" "{tts}" "{voices}" "{mode}" "{timeout}"')
    assert r.returncode == 0, r.stdout + r.stderr
    return r


def effective(home: Path, key: str) -> list:
    """What the installed voice_mode.config reads for `key` from this HOME's voicemode.env."""
    code = ("import os; os.chdir(os.environ['HOME']); from voice_mode import config; "
            f"print(','.join(config.{key}))")
    r = subprocess.run([config_python(), "-c", code], env=clean_env(home), capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().split(",")


def write(home: Path, text: str) -> None:
    envfile(home).parent.mkdir(exist_ok=True)
    envfile(home).write_text(text)


# -- the bug: the block is shadowed ------------------------------------------------

def test_voice_mode_reads_the_blocks_stt_list_not_an_earlier_template_line(tmp_path):
    # RED before the fix: voice_mode.config returns [2022] because line 5 comes first.
    write(tmp_path, TEMPLATE)
    update_env(tmp_path, f"{S2022},{S2023}", f"{K8880},{P8881}")
    assert effective(tmp_path, "STT_BASE_URLS") == [S2022, S2023]
    assert effective(tmp_path, "TTS_BASE_URLS") == [K8880, P8881]


def test_the_shadowing_lines_are_commented_in_place_with_the_marker(tmp_path):
    write(tmp_path, TEMPLATE)
    update_env(tmp_path, f"{S2022},{S2023}", f"{K8880},{P8881}")
    lines = envfile(tmp_path).read_text().splitlines()
    assert lines[3] == f"{MARK}VOICEMODE_TTS_BASE_URLS={K8880}"
    assert lines[4] == f"{MARK}VOICEMODE_STT_BASE_URLS={S2022}"


def test_every_other_line_is_left_exactly_as_it_was(tmp_path):
    # Comments, commented defaults, the user's voice, a multiline value and an export line stay.
    write(tmp_path, TEMPLATE)
    update_env(tmp_path, f"{S2022},{S2023}", f"{K8880},{P8881}")
    after = envfile(tmp_path).read_text().splitlines()
    before = TEMPLATE.splitlines()
    for i, line in enumerate(before):
        if i in (3, 4):
            continue
        assert after[i] == line, (i, line, after[i])
    assert "VOICEMODE_VOICES=am_puck" in after              # the user's voice is still active
    assert not any(l.startswith("VOICEMODE_VOICES=") for l in after[after.index(BEGIN):])


def test_a_line_inside_a_multiline_value_is_not_mistaken_for_a_key(tmp_path):
    text = ("VOICEMODE_PRONOUNCE='\n"
            f"VOICEMODE_STT_BASE_URLS={S2022}\n"
            "'\n")
    write(tmp_path, text)
    update_env(tmp_path, f"{S2022},{S2023}", K8880)
    assert envfile(tmp_path).read_text().startswith(text)


def test_a_duplicate_after_the_block_is_superseded_too(tmp_path):
    write(tmp_path, f"A=1\n{BEGIN}\n# mode: x\nVOICEMODE_STT_BASE_URLS={S2022}\n{END}\n"
                    f"VOICEMODE_STT_BASE_URLS=http://elsewhere/v1\n")
    update_env(tmp_path, f"{S2022},{S2023}", K8880)
    assert f"{MARK}VOICEMODE_STT_BASE_URLS=http://elsewhere/v1\n" in envfile(tmp_path).read_text()


def test_a_re_run_leaves_the_file_byte_identical(tmp_path):
    write(tmp_path, TEMPLATE)
    update_env(tmp_path, f"{S2022},{S2023}", f"{K8880},{P8881}")
    once = envfile(tmp_path).read_bytes()
    r = update_env(tmp_path, f"{S2022},{S2023}", f"{K8880},{P8881}")
    assert envfile(tmp_path).read_bytes() == once
    assert "already set" in r.stdout


def test_the_switch_says_which_lines_it_superseded(tmp_path):
    write(tmp_path, TEMPLATE)
    r = update_env(tmp_path, f"{S2022},{S2023}", K8880)
    assert "VOICEMODE_STT_BASE_URLS (line 5)" in r.stdout, r.stdout
    assert "VOICEMODE_TTS_BASE_URLS (line 4)" in r.stdout, r.stdout


# -- restoring ---------------------------------------------------------------------

def test_a_superseded_line_comes_back_when_the_block_no_longer_writes_its_key(tmp_path):
    write(tmp_path, TEMPLATE)
    update_env(tmp_path, f"{S2022},{S2023}", f"{K8880},{P8881}")
    update_env(tmp_path, "", f"{K8880},{P8881}")      # this block writes no STT list
    lines = envfile(tmp_path).read_text().splitlines()
    assert lines[4] == f"VOICEMODE_STT_BASE_URLS={S2022}"
    assert lines[3] == f"{MARK}VOICEMODE_TTS_BASE_URLS={K8880}"   # still written by the block


def test_only_the_exact_marker_is_restored(tmp_path):
    # (Text after the exact marker is the original line verbatim, indentation included.)
    near = [f"# Superseded by voicemode-switch managed block: VOICEMODE_STT_BASE_URLS={S2022}",
            f" # superseded by voicemode-switch managed block: VOICEMODE_STT_BASE_URLS={S2022}",
            f"# superseded by voicemode-switch managed block VOICEMODE_STT_BASE_URLS={S2022}",
            f"# superseded by hand: VOICEMODE_STT_BASE_URLS={S2022}",
            f"{MARK}# VOICEMODE_STT_BASE_URLS={S2022}"]          # a comment stays a comment
    write(tmp_path, "\n".join(near) + "\n")
    update_env(tmp_path, "", K8880)
    lines = envfile(tmp_path).read_text().splitlines()
    assert lines[:len(near)] == near


# -- a value the user set ----------------------------------------------------------

def test_a_tts_timeout_the_user_set_stays_active_and_wins(tmp_path):
    write(tmp_path, "VOICEMODE_TTS_TIMEOUT_LOCAL=120\n")
    update_env(tmp_path, S2022, K8880, timeout="90")
    text = envfile(tmp_path).read_text()
    assert text.startswith("VOICEMODE_TTS_TIMEOUT_LOCAL=120\n")
    assert "VOICEMODE_TTS_TIMEOUT_LOCAL=90" not in text


# -- the check that would have caught it -------------------------------------------

def test_verify_routing_warns_naming_the_shadowing_key_and_line(tmp_path):
    write(tmp_path, f"# c\nVOICEMODE_STT_BASE_URLS={S2022}\n{BEGIN}\n# mode: local\n"
                    f"VOICEMODE_STT_BASE_URLS={S2022},{S2023}\nVOICEMODE_TTS_BASE_URLS={K8880}\n{END}\n")
    r = sourced(tmp_path, "verify_routing", VOICEMODE_CONFIG_PY=config_python())
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert f"STT: {S2022}" in out                       # the effective list, as voice-mode reads it
    assert f"TTS: {K8880}" in out
    warn = [l for l in out.splitlines() if "shadow" in l]
    assert len(warn) == 1, out
    assert "VOICEMODE_STT_BASE_URLS" in warn[0] and "line 2" in warn[0], warn[0]


def test_verify_routing_is_quiet_when_the_block_is_what_voice_mode_reads(tmp_path):
    write(tmp_path, f"{BEGIN}\n# mode: local\nVOICEMODE_STT_BASE_URLS={S2022},{S2023}\n"
                    f"VOICEMODE_TTS_BASE_URLS={K8880}\n{END}\n")
    r = sourced(tmp_path, "verify_routing", VOICEMODE_CONFIG_PY=config_python())
    assert r.returncode == 0, r.stderr
    assert f"STT: {S2022}, {S2023}" in r.stdout
    assert "shadow" not in r.stdout


def test_verify_routing_without_voice_mode_says_so_and_does_not_fail(tmp_path):
    write(tmp_path, f"{BEGIN}\nVOICEMODE_STT_BASE_URLS={S2022}\n{END}\n")
    r = sourced(tmp_path, "verify_routing", VOICEMODE_CONFIG_PY="/nonexistent/python")
    assert r.returncode == 0, r.stderr
    assert "not checked" in r.stdout


def test_a_mode_switch_ends_with_the_effective_routing(tmp_path):
    write(tmp_path, TEMPLATE)
    r = subprocess.run([str(SWITCH), "localonly"], capture_output=True, text=True, timeout=120,
                       env=clean_env(tmp_path, VOICEMODE_CONFIG_PY=config_python()))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Effective routing" in r.stdout and "shadow" not in r.stdout, r.stdout
