"""patches/tts_chunks.py: a long reply is spoken in parts, one request each.

2026-11-14, Windows laptop: a 1,178-character reply (about 54 s of audio) went to the
local Kokoro server as one request. The laptop generates 2.5-5x slower than real time,
so the request took 134.7 s and timed out at 90 s, and the reply was never spoken.
Split into parts of at most VOICEMODE_TTS_CHUNK_CHARS characters, every request stays
well inside the timeout, and the first part plays sooner.

Run: pytest tests/test_tts_chunks.py
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "tts_chunks.py"
_spec = importlib.util.spec_from_file_location("tts_chunks_under_test", MODULE)
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

# The size of the reply in the field report: 1,178 characters (that one had 184 words).
LONG = (
    "Here is where things stand on the migration. The content package built cleanly, and the "
    "three failing checks from yesterday now pass, because the dispatcher rule that blocked the "
    "image renditions was removed. Two things are still open. First, the author instance still "
    "points at the old asset store, so new uploads land in the wrong place; that needs a change "
    "to the cloud configuration, which only the customer's administrator can make, and I have "
    "asked them for it in the project channel. Second, the search index is about a day behind, "
    "which is expected after a full reindex, and it should catch up overnight without any action "
    "from us. I also looked at the performance numbers you asked about: page response times on "
    "the publish tier are back under the agreed limit, with the slowest template at about eight "
    "hundred milliseconds, down from two and a half seconds last week. If you want, I can write "
    "this up for the customer this very afternoon, or we can wait for the administrator's change and "
    "send one complete update tomorrow morning instead. Tell me which you prefer, and whether "
    "the customer should see the performance figures or only the summary of what changed today."
)


def words(parts):
    return " ".join(parts).split()


def test_the_field_reply_is_the_measured_size():
    assert len(LONG) == 1178


def test_short_text_is_one_part_unchanged():
    assert tc.split_for_tts("Hello there. How are you?", 200) == ["Hello there. How are you?"]


@pytest.mark.parametrize("limit", [0, -5])
def test_a_limit_of_zero_or_less_means_one_part(limit):
    assert tc.split_for_tts(LONG, limit) == [LONG]


@pytest.mark.parametrize("limit", [60, 120, 200, 300])
def test_long_text_splits_into_parts_no_longer_than_the_limit(limit):
    parts = tc.split_for_tts(LONG, limit)
    assert len(parts) > 1
    assert all(0 < len(p) <= limit for p in parts), [len(p) for p in parts]
    assert words(parts) == LONG.split(), "no word may be lost, added or reordered"


def test_parts_end_at_sentence_ends_when_sentences_fit():
    parts = tc.split_for_tts(LONG, 300)
    assert all(p[-1] in ".?!" for p in parts), parts


def test_a_sentence_longer_than_the_limit_splits_at_clauses_then_words():
    sentence = ("First, the author instance still points at the old asset store, so new uploads "
                "land in the wrong place; that needs a change to the cloud configuration.")
    parts = tc.split_for_tts(sentence, 70)
    assert all(len(p) <= 70 for p in parts)
    assert parts[0].endswith(",") or parts[0].endswith(";")
    assert words(parts) == sentence.split()


def test_one_word_longer_than_the_limit_is_cut():
    parts = tc.split_for_tts("a " + "x" * 50 + " b", 20)
    assert all(len(p) <= 20 for p in parts)
    assert "".join(parts).replace(" ", "") == "a" + "x" * 50 + "b"


def test_lines_without_full_stops_are_separate_sentences():
    text = "Open items\n- the asset store\n- the search index\nThat is all."
    parts = tc.split_for_tts(text, 30)
    assert all(len(p) <= 30 for p in parts)
    assert words(parts) == text.split()


@pytest.mark.parametrize("raw,expected", [(None, 0), ("", 0), ("  ", 0), ("250", 250),
                                           ("0", 0), ("-1", 0), ("lots", 0), ("12.5", 0)])
def test_chunk_chars_setting(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(tc.SETTING, raising=False)
    else:
        monkeypatch.setenv(tc.SETTING, raw)
    assert tc.chunk_chars() == expected


def test_the_setting_is_off_by_default(monkeypatch):
    monkeypatch.delenv(tc.SETTING, raising=False)
    assert tc.SETTING == "VOICEMODE_TTS_CHUNK_CHARS"
    assert tc.chunk_chars() == 0


# --- speak_in_chunks: drives the real (whole-text) failover once per part ----------------

class FakeWhole:
    """simple_tts_failover's original body: records each call, answers from a script."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    async def __call__(self, text, voice, model=None, **kwargs):
        self.calls.append({"text": text, "voice": voice, "model": model, "kwargs": dict(kwargs)})
        kwargs.pop("ref_text", None)  # the real one pops it: each part must get its own copy
        n = len(self.calls)
        answer = self.answers.get(n)
        if answer == "fail":
            return False, None, {"error_type": "all_providers_failed", "attempted_endpoints": [
                {"endpoint": "http://127.0.0.1:18880/v1/audio/speech", "provider": "kokoro",
                 "error": "Request timed out."}]}
        metrics = {"ttfa": 0.5 * n, "generation": 1.0, "playback": 2.0, "audio_path": f"p{n}.wav"}
        if answer:
            metrics[answer] = True
        return True, metrics, {"base_url": "http://127.0.0.1:18880/v1", "voice": voice, "part": n}


def run(whole, text, monkeypatch, limit=200, **kwargs):
    monkeypatch.setenv(tc.SETTING, str(limit))
    return asyncio.run(tc.speak_in_chunks(whole, text, "af_sky", None, kwargs))


def test_each_part_is_spoken_in_order_and_the_metrics_add_up(monkeypatch):
    whole = FakeWhole()
    ok, metrics, config = run(whole, LONG, monkeypatch, ref_text="r", speed=1.1)
    parts = tc.split_for_tts(LONG, 200)
    assert ok
    assert [c["text"] for c in whole.calls] == parts
    assert all(c["kwargs"] == {"ref_text": "r", "speed": 1.1} for c in whole.calls)
    assert metrics["ttfa"] == 0.5, "time to first audio is the first part's"
    assert metrics["generation"] == pytest.approx(1.0 * len(parts))
    assert metrics["playback"] == pytest.approx(2.0 * len(parts))
    assert metrics["audio_path"] == f"p{len(parts)}.wav"
    assert config["part"] == 1


def test_short_text_is_passed_through_untouched(monkeypatch):
    whole = FakeWhole()
    result = run(whole, "Hello.", monkeypatch, ref_text="r")
    assert len(whole.calls) == 1 and whole.calls[0]["text"] == "Hello."
    assert result == (True, {"ttfa": 0.5, "generation": 1.0, "playback": 2.0, "audio_path": "p1.wav"},
                      {"base_url": "http://127.0.0.1:18880/v1", "voice": "af_sky", "part": 1})


def test_off_means_one_request_for_the_whole_text(monkeypatch):
    whole = FakeWhole()
    run(whole, LONG, monkeypatch, limit=0)
    assert [c["text"] for c in whole.calls] == [LONG]


@pytest.mark.parametrize("flag", ["control_stopped", "transport_interrupted"])
def test_a_stop_or_skip_ends_the_reply_after_that_part(monkeypatch, flag):
    """Barge-in, a push-to-talk hold and the pill's Stop all arrive as control_stopped:
    the rest of the reply must not be spoken."""
    whole = FakeWhole({2: flag})
    ok, metrics, _ = run(whole, LONG, monkeypatch)
    assert ok and metrics[flag] is True
    assert len(whole.calls) == 2


def test_a_failed_part_stops_the_reply_and_says_which_part(monkeypatch):
    whole = FakeWhole({3: "fail"})
    ok, metrics, config = run(whole, LONG, monkeypatch)
    n = len(tc.split_for_tts(LONG, 200))
    assert not ok and metrics is None
    assert len(whole.calls) == 3, "nothing after the failed part is sent"
    error = config["attempted_endpoints"][0]["error"]
    assert error.startswith("Request timed out.")
    assert f"part 3 of {n}" in error and "2 parts were spoken" in error
