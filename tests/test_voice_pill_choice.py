"""The pill's voice choice at converse time (FELIX-MAC, 2026-11-18, n=1 each).

BUG 1: the pill starts with a chat's first converse, so that call's voice was resolved before
any choice could reach it. The backend now keeps the last choice (last_choice.json), used until
a session has its own override. Off: VOICEMODE_PILL_REMEMBER_LAST=false.

BUG 2: a pinned German voice read Spanish text. The voice now follows the language of the text:
a caller voice of another language is kept and the agent is told. Off (replace always, as
before): VOICEMODE_PILL_VOICE_FOLLOWS_LANGUAGE=false.

State goes to tmp_path only, settings come from a throwaway HOME and the environment.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PATCHES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


st = _load("voice_pill_state")
vp = _load("voice_pill")


@pytest.fixture
def base(tmp_path, monkeypatch):
    """Settings from a throwaway HOME only; am_puck is the install's default voice."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in ("VOICEMODE_PILL_VOICE_FOLLOWS_LANGUAGE", "VOICEMODE_PILL_REMEMBER_LAST",
                "VOICEMODE_TTS_VOICE", "VOICEMODE_BASE_DIR", "VOICEMODE_VOICE_BARGE",
                "VOICEMODE_PILL_LANGUAGES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VOICEMODE_VOICES", "am_puck,af_sky")
    # No barge note: it depends on this machine's mic reader, not on the choice.
    monkeypatch.setattr(st, "capture_backend", lambda: None)
    getattr(st, "_KEPT_NOTES", {}).clear()
    return tmp_path / "pill"


PID = os.getpid()


# -- the last choice ----------------------------------------------------------------------

def test_last_choice_round_trips(base):
    assert st.get_last_choice(base=base) == {}
    st.set_last_choice("dm_martin", "German", base=base)
    assert st.get_last_choice(base=base) == {"voice": "dm_martin", "language": "German"}
    st.set_last_choice(None, "Spanish", base=base)
    assert st.get_last_choice(base=base) == {"language": "Spanish"}


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'{"voice": 5, "language": ["x"]}', b"",
                                 b"\xff\xfe"])
def test_a_corrupt_last_choice_reads_as_none(base, raw):
    base.mkdir(parents=True, exist_ok=True)
    (base / "last_choice.json").write_bytes(raw)
    assert st.get_last_choice(base=base) == {}
    assert st.effective_voice(PID, "am_puck", base=base) == "am_puck"


def test_a_first_call_without_an_override_uses_the_last_choice(base):
    st.set_last_choice("dm_martin", "German", base=base)
    assert st.get_override(PID, base=base) == {}
    assert st.effective_voice(PID, None, base=base) == "dm_martin"
    assert st.effective_voice(PID, "am_puck", base=base) == "dm_martin"
    assert "Write your next messages in German." in st.result_note(PID, base=base)


def test_without_a_last_choice_a_first_call_is_unchanged(base):
    assert st.effective_voice(PID, "am_puck", base=base) == "am_puck"
    assert st.effective_voice(PID, None, base=base) is None
    assert st.result_note(PID, base=base) == ""


def test_the_session_override_wins_over_the_last_choice(base):
    st.set_last_choice("dm_martin", "German", base=base)
    st.set_override(PID, "bf_emma", "English", base=base)
    assert st.effective_voice(PID, None, base=base) == "bf_emma"
    assert "Write your next messages in English." in st.result_note(PID, base=base)


@pytest.mark.parametrize("off", ["false", "0", "no", "off", " FALSE "])
def test_remember_last_off_ignores_the_last_choice(base, monkeypatch, off):
    st.set_last_choice("dm_martin", "German", base=base)
    monkeypatch.setenv("VOICEMODE_PILL_REMEMBER_LAST", off)
    assert st.effective_voice(PID, "am_puck", base=base) == "am_puck"
    assert st.result_note(PID, base=base) == ""


def test_remember_last_is_read_from_voicemode_env_too(base, tmp_path):
    st.set_last_choice("dm_martin", "German", base=base)
    home = tmp_path / "home" / ".voicemode"
    home.mkdir(parents=True)
    (home / "voicemode.env").write_text("VOICEMODE_PILL_REMEMBER_LAST=false\n")
    assert st.effective_voice(PID, "am_puck", base=base) == "am_puck"


# -- the voice follows the language of the text -------------------------------------------

def test_a_same_language_caller_voice_is_replaced(base):
    st.set_override(PID, "dm_martin", "German", base=base)
    assert st.effective_voice(PID, "p_de_eva", base=base) == "dm_martin"
    assert "Spoken with your voice" not in st.result_note(PID, base=base)


def test_a_caller_voice_of_another_language_is_kept_and_the_agent_told(base):
    st.set_override(PID, "dm_martin", "German", base=base)
    assert st.effective_voice(PID, "em_alex", base=base) == "em_alex"
    note = st.result_note(PID, base=base)
    assert "em_alex (Spanish)" in note and "dm_martin (German)" in note
    assert "The user selected German." in note        # the pill's language stays the hint
    assert "em_alex" not in st.result_note(PID, base=base)   # told once, for that turn


def test_a_caller_voice_of_another_language_is_kept_over_the_last_choice(base):
    st.set_last_choice("dm_martin", "German", base=base)
    assert st.effective_voice(PID, "ef_dora", base=base) == "ef_dora"
    assert "ef_dora (Spanish)" in st.result_note(PID, base=base)


def test_a_later_replaced_turn_clears_the_kept_note(base):
    st.set_override(PID, "dm_martin", "German", base=base)
    st.effective_voice(PID, "em_alex", base=base)
    st.effective_voice(PID, None, base=base)          # the next turn, before its result
    assert "em_alex" not in st.result_note(PID, base=base)


def test_no_caller_voice_or_the_default_voice_is_replaced(base):
    st.set_override(PID, "dm_martin", "German", base=base)
    assert st.effective_voice(PID, None, base=base) == "dm_martin"
    assert st.effective_voice(PID, "am_puck", base=base) == "dm_martin"   # the install default


def test_the_default_is_upstreams_tts_voices_as_converse_sees_it(base, monkeypatch):
    # The server: converse's own TTS_VOICES (config_reload may have changed it since start).
    fake = type(sys)("voice_mode.tools.converse")
    fake.TTS_VOICES = ["bf_emma", "af_sky"]
    monkeypatch.setitem(sys.modules, "voice_mode.tools.converse", fake)
    st.set_override(PID, "dm_martin", "German", base=base)
    assert st.effective_voice(PID, "bf_emma", base=base) == "dm_martin"   # upstream's default
    assert st.effective_voice(PID, "am_puck", base=base) == "am_puck"     # not, here: kept


def test_without_upstream_the_default_comes_from_the_settings(base, monkeypatch):
    monkeypatch.delitem(sys.modules, "voice_mode.tools.converse", raising=False)
    monkeypatch.delitem(sys.modules, "voice_mode.config", raising=False)
    st.set_override(PID, "dm_martin", "German", base=base)
    assert st.effective_voice(PID, "am_puck", base=base) == "dm_martin"   # VOICEMODE_VOICES


def test_an_empty_upstream_list_falls_back_to_the_settings(base, monkeypatch):
    fake = type(sys)("voice_mode.tools.converse")
    fake.TTS_VOICES = []
    monkeypatch.setitem(sys.modules, "voice_mode.tools.converse", fake)
    st.set_override(PID, "dm_martin", "German", base=base)
    assert st.effective_voice(PID, "am_puck", base=base) == "dm_martin"


@pytest.mark.parametrize("caller,pinned", [("nova", "dm_martin"), ("em_alex", "custom_voice")])
def test_an_unknown_language_replaces_as_before(base, caller, pinned):
    st.set_override(PID, pinned, None, base=base)
    assert st.effective_voice(PID, caller, base=base) == pinned


def test_follows_language_off_replaces_always(base, monkeypatch):
    monkeypatch.setenv("VOICEMODE_PILL_VOICE_FOLLOWS_LANGUAGE", "false")
    st.set_override(PID, "dm_martin", "German", base=base)
    assert st.effective_voice(PID, "em_alex", base=base) == "dm_martin"
    assert "em_alex" not in st.result_note(PID, base=base)


def test_a_language_only_override_keeps_the_caller_voice(base):
    st.set_override(PID, None, "Spanish", base=base)
    assert st.effective_voice(PID, "em_alex", base=base) == "em_alex"


# -- the backend writes the last choice ---------------------------------------------------

@pytest.fixture
def ctx(base, tmp_path, monkeypatch):
    monkeypatch.setattr(vp, "_install_dirs", lambda: [])
    monkeypatch.setattr(vp, "_fetch_voices", lambda url: None, raising=False)
    monkeypatch.setattr(vp, "_VOICES_CACHE", {}, raising=False)
    monkeypatch.setattr(vp, "BACKGROUND_REFRESH", False, raising=False)
    return vp.Context(base=base, home=tmp_path / "vm", sock=tmp_path / "no.sock", switch=None)


def test_set_voice_and_language_remember_the_last_choice(ctx):
    st.register_session(PID, "felix", "am_puck", ctx.base)
    st._write_json(ctx.base / "last_floor.json", {"pid": PID, "label": "felix"})
    vp.handle_command({"cmd": "set_voice", "voice": "dm_martin"}, ctx)
    assert st.get_last_choice(ctx.base) == {"voice": "dm_martin"}
    vp.handle_command({"cmd": "set_language", "language": "German"}, ctx)
    assert st.get_last_choice(ctx.base) == {"voice": "dm_martin", "language": "German"}


def test_a_choice_without_a_floor_is_not_remembered(ctx):
    vp.handle_command({"cmd": "set_voice", "voice": "dm_martin"}, ctx)
    assert st.get_last_choice(ctx.base) == {}


def test_the_real_timeline_first_call_then_set_voice_then_second_call(ctx):
    """The Mac run: 17:41:33 the first converse resolves its voice (the pill is not up yet);
    17:41:48 set_voice + set_language; 17:42:27 the second converse uses dm_martin. A new
    chat's first call then gets dm_martin at once."""
    st.register_session(PID, "felix", "am_puck", ctx.base)
    assert st.effective_voice(PID, None, ctx.base) is None     # upstream then speaks am_puck
    st._write_json(ctx.base / "last_floor.json", {"pid": PID, "label": "felix"})
    vp.handle_command({"cmd": "set_voice", "voice": "dm_martin"}, ctx)
    vp.handle_command({"cmd": "set_language", "language": "German"}, ctx)
    assert st.effective_voice(PID, None, ctx.base) == "dm_martin"
    st.set_override(PID, None, None, ctx.base)                 # a new session: no override yet
    assert st.effective_voice(PID, "am_puck", ctx.base) == "dm_martin"


# -- a partial change starts from the choice in force (Talos on FELIX #1910, 2026-11-19) ---
# A new chat inherits last_choice.json but has no override. A voice-only change built its
# override from the empty get_override(), so the remembered language was lost (and the reverse).

def _floor_session(ctx, voice="am_puck"):
    st.register_session(PID, "felix", voice, ctx.base)
    st._write_json(ctx.base / "last_floor.json", {"pid": PID, "label": "felix"})


def test_a_voice_only_change_in_a_new_session_keeps_the_remembered_language(ctx):
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    vp.handle_command({"cmd": "set_voice", "voice": "p_de_thorsten"}, ctx)
    want = {"voice": "p_de_thorsten", "language": "German"}
    assert st.get_override(PID, ctx.base) == want
    assert st.get_last_choice(ctx.base) == want
    assert st.pill_choice(PID, ctx.base) == want


def test_a_language_only_change_in_a_new_session_keeps_the_remembered_voice(ctx):
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    vp.handle_command({"cmd": "set_language", "language": "Spanish"}, ctx)
    want = {"voice": "dm_martin", "language": "Spanish"}
    assert st.get_override(PID, ctx.base) == want
    assert st.get_last_choice(ctx.base) == want


def test_choosing_german_keeps_a_remembered_german_voice(ctx):
    # German needs a German voice: the voice in force is the remembered p_de_thorsten, not the
    # session's own am_puck, so it is not swapped for dm_martin.
    st.set_last_choice("p_de_thorsten", "English", ctx.base)
    _floor_session(ctx)
    vp.handle_command({"cmd": "set_language", "language": "German"}, ctx)
    assert st.get_override(PID, ctx.base) == {"voice": "p_de_thorsten", "language": "German"}


def test_a_partial_change_without_a_saved_choice_is_unchanged(ctx):
    _floor_session(ctx)
    vp.handle_command({"cmd": "set_voice", "voice": "p_de_thorsten"}, ctx)
    assert st.get_override(PID, ctx.base) == {"voice": "p_de_thorsten"}
    assert st.get_last_choice(ctx.base) == {"voice": "p_de_thorsten"}


def test_a_partial_change_with_remember_off_is_unchanged(ctx, monkeypatch):
    monkeypatch.setenv("VOICEMODE_PILL_REMEMBER_LAST", "false")
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    vp.handle_command({"cmd": "set_voice", "voice": "p_de_thorsten"}, ctx)
    assert st.get_override(PID, ctx.base) == {"voice": "p_de_thorsten"}
    assert st.get_last_choice(ctx.base) == {"voice": "p_de_thorsten"}


def test_a_partial_change_over_a_corrupt_saved_choice_writes_a_good_one(ctx):
    ctx.base.mkdir(parents=True, exist_ok=True)
    (ctx.base / "last_choice.json").write_bytes(b"{not json")
    _floor_session(ctx)
    vp.handle_command({"cmd": "set_voice", "voice": "p_de_thorsten"}, ctx)
    assert st.get_override(PID, ctx.base) == {"voice": "p_de_thorsten"}
    assert st.get_last_choice(ctx.base) == {"voice": "p_de_thorsten"}


def test_a_partial_change_starts_from_the_session_override_first(ctx):
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    st.set_override(PID, "bf_emma", "English", ctx.base)
    vp.handle_command({"cmd": "set_voice", "voice": "bm_george"}, ctx)
    assert st.get_override(PID, ctx.base) == {"voice": "bm_george", "language": "English"}
    assert st.get_last_choice(ctx.base) == {"voice": "bm_george", "language": "English"}


# -- the window shows the choice in force (Talos on FELIX #1910, 2026-11-19) ---------------

def test_the_window_shows_the_saved_choice_without_an_override(ctx):
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    s = vp.gather(ctx)
    assert (s["voice"], s["language"]) == ("dm_martin", "German")


def test_the_window_shows_the_override_over_the_saved_choice(ctx):
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    st.set_override(PID, "bf_emma", "English", ctx.base)
    s = vp.gather(ctx)
    assert (s["voice"], s["language"]) == ("bf_emma", "English")


def test_the_window_ignores_the_saved_choice_with_remember_off(ctx, monkeypatch):
    monkeypatch.setenv("VOICEMODE_PILL_REMEMBER_LAST", "false")
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    s = vp.gather(ctx)
    assert (s["voice"], s["language"]) == ("am_puck", "English")


def test_an_explicit_spanish_turn_over_a_remembered_german_agrees_everywhere(ctx):
    """Talos's scenario: remembered German, no override, the agent passes ef_dora for Spanish
    text. The caller's voice is kept for that turn; the pill's choice stays German, and the
    window, the recognition language and the hint all say German."""
    st.set_last_choice("dm_martin", "German", ctx.base)
    _floor_session(ctx)
    # What enter() does: resolve the voice, then register the session with it.
    spoken = st.effective_voice(PID, "ef_dora", ctx.base)
    assert spoken == "ef_dora"
    st.register_session(PID, "felix", spoken, ctx.base)
    assert st.pill_choice(PID, ctx.base).get("language") == "German"   # what Whisper is given
    note = st.result_note(PID, base=ctx.base)
    assert "Write your next messages in German." in note
    assert "ef_dora (Spanish), not the pill's dm_martin (German)" in note
    s = vp.gather(ctx)
    assert (s["voice"], s["language"]) == ("dm_martin", "German")
