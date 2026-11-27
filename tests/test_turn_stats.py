"""patches/turn_stats.py: per-turn numbers without audio (FELIX #2107)."""
import importlib.util
from pathlib import Path

PATCHES = Path(__file__).resolve().parent.parent / "patches"
spec = importlib.util.spec_from_file_location("turn_stats", PATCHES / "turn_stats.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


def feed(stats, pattern, loud=3000.0, quiet=30.0):
    for ch in pattern:
        stats.frame(ch == "S", loud if ch == "S" else quiet)


def test_fields_are_fixed_and_null_when_nothing_was_recorded():
    ts.event_fields()  # drain
    out = ts.event_fields()
    assert list(out) == list(ts.FIELDS)
    assert all(v is None for v in out.values())


def test_counts_after_the_first_speech_frame():
    s = ts.start(1000)
    feed(s, "...." + "SSSS" + "." * 12 + "SS" + "." * 34)   # waiting, speech, 360 ms pause, speech, end pause
    ts.finish(s, "silence")
    f = ts.event_fields()
    assert f["end_reason"] == "silence"
    assert f["frames"] == 4 + 4 + 12 + 2 + 34
    assert f["pause_resets"] == 1                  # the 12-frame pause was cut by speech
    assert f["longest_pause_ms"] == 34 * 30
    assert f["speech_share"] == round(6 / (4 + 12 + 2 + 34), 3)
    assert f["silence_needed_ms"] == 1000
    assert -70 < f["quiet_dbfs"] < -60             # 30 of 32768 is about -60.8 dBFS


def test_a_short_pause_is_not_a_reset():
    s = ts.start()
    feed(s, "SS" + "." * 9 + "SS")
    ts.finish(s, "done")
    assert ts.event_fields()["pause_resets"] == 0


def test_first_reason_wins_and_unknown_reasons_are_ignored():
    s = ts.start()
    s.end("bogus")
    s.end("done")
    ts.finish(s, "cap")
    assert ts.event_fields()["end_reason"] == "done"


def test_numbers_are_consumed_once():
    s = ts.start()
    feed(s, "SS")
    ts.finish(s, "stop")
    assert ts.event_fields()["end_reason"] == "stop"
    assert ts.event_fields()["end_reason"] is None   # the next turn never reports these


def test_no_vad_recording():
    ts.no_vad()
    f = ts.event_fields()
    assert f["end_reason"] == "no_vad" and f["frames"] == 0 and f["quiet_dbfs"] is None


def test_levels_are_bounded():
    assert ts._dbfs(0) == -90.0 and ts._dbfs(float("nan")) == -90.0 and ts._dbfs("x") == -90.0
    assert ts._dbfs(32768) == 0.0


def test_no_audio_is_kept():
    s = ts.start()
    feed(s, "S." * 100)
    f = s.fields()
    assert all(isinstance(v, (int, float, str, type(None))) for v in f.values())


# Review of a51c406 (F1): numbers nobody consumed must never be reported for a later turn.
def test_a_new_recording_drops_numbers_nobody_consumed():
    a = ts.start()
    feed(a, "SS")
    ts.finish(a, "cancel")           # an ESC'd turn: no RECORDING_END consumed it
    b = ts.start()                   # the next recording
    feed(b, "S" * 5)
    ts.finish(b, "silence")
    assert ts.event_fields()["end_reason"] == "silence"


def test_a_recording_that_never_finished_counting_is_a_fallback():
    s = ts.start()                   # the stream failed to open; a fixed-length recording ran
    feed(s, "S")
    f = ts.event_fields()
    assert f["end_reason"] == "fallback" and f["frames"] == 1
    assert ts.listening_since() is None


def test_a_late_finish_from_a_cancelled_recording_is_ignored():
    old = ts.start()
    new = ts.start()
    ts.finish(old, "cancel")         # the cancelled thread ends after the new turn began
    feed(new, "SS")
    ts.finish(new, "done")
    assert ts.event_fields()["end_reason"] == "done"


def test_listening_since_is_the_current_recording_start():
    import time
    ts.event_fields()
    before = time.time()
    s = ts.start()
    assert before <= ts.listening_since() <= time.time()
    ts.finish(s, "silence")
    assert ts.listening_since() is None


def test_inner_pause_excludes_the_closing_pause():
    s = ts.start(1000)
    feed(s, "SSS" + "." * 20 + "SS" + "." * 34)
    ts.finish(s, "silence")
    f = ts.event_fields()
    assert f["longest_inner_pause_ms"] == 20 * 30 and f["longest_pause_ms"] == 34 * 30
