"""Behaviour of the voice barge-in trigger, with the mic, Silero and socket faked."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "voice_barge.py"
spec = importlib.util.spec_from_file_location("voice_barge", MODULE)
voice_barge = importlib.util.module_from_spec(spec)
sys.modules["voice_barge"] = voice_barge
spec.loader.exec_module(voice_barge)

SPEECH, NOT_SPEECH = 0.9, 0.1
LOUD, ECHO = -20.0, -55.0


def detector(need=3):
    return voice_barge.BargeDetector(gate_db=-35.0, need_windows=need, threshold=0.5)


def feed(det, *frames):
    return [det.feed(prob, db) for prob, db in frames]


class TestBargeDetector:
    def test_fires_after_enough_consecutive_loud_speech_windows(self):
        assert feed(detector(), *[(SPEECH, LOUD)] * 3) == [False, False, True]

    def test_echo_flagged_as_speech_stays_below_the_gate(self):
        assert not any(feed(detector(), *[(SPEECH, ECHO)] * 100))

    def test_loud_noise_silero_rejects_never_fires(self):
        assert not any(feed(detector(), *[(NOT_SPEECH, LOUD)] * 100))

    def test_a_short_burst_does_not_fire(self):
        frames = [(SPEECH, LOUD), (SPEECH, LOUD), (SPEECH, ECHO)] * 20
        assert not any(feed(detector(), *frames))

    def test_fires_only_once_until_reset(self):
        det = detector(need=2)
        assert sum(feed(det, *[(SPEECH, LOUD)] * 10)) == 1
        det.reset()
        assert sum(feed(det, *[(SPEECH, LOUD)] * 2)) == 1


def test_window_db_measures_full_scale_and_silence():
    assert voice_barge.window_db(np.ones(512, dtype=np.float32)) == pytest.approx(0.0, abs=0.01)
    assert voice_barge.window_db(np.zeros(512, dtype=np.float32)) < -150


class FakeVad:
    """Returns whatever probability the capture queued with the current window."""

    def __init__(self):
        self.resets = 0
        self.next_prob = 0.0

    def reset(self):
        self.resets += 1

    def prob(self, window):
        return self.next_prob


class FakeCapture:
    def __init__(self, frames, vad):
        self.frames = frames
        self.vad = vad
        self.closed = False

    def read_window(self):
        if not self.frames:
            return None
        prob, db = self.frames.pop(0)
        self.vad.next_prob = prob
        return np.full(512, 10 ** (db / 20), dtype=np.float32)

    def close(self):
        self.closed = True


class Harness:
    """Drives run() with a scripted `speaking` timeline."""

    def __init__(self, speaking, frames, warmup=0, paused=None, on_fire=None):
        self.paused = paused
        self.on_fire = on_fire
        self.speaking = list(speaking)
        self.frames = frames
        self.opened = []
        self.barges = 0
        self.warmup = warmup
        self.vad = FakeVad()

    def poll(self):
        return self.speaking.pop(0) if self.speaking else None  # None ends the run

    def open_capture(self):
        cap = FakeCapture(self.frames, self.vad)
        self.opened.append(cap)
        return cap

    def barge(self):
        self.barges += 1

    def run(self):
        voice_barge.run(
            poll_speaking=self.poll,
            open_capture=self.open_capture,
            vad=self.vad,
            detector=detector(),
            send_barge=self.barge,
            sleep=lambda _s: None,
            warmup_windows=self.warmup,
            poll_every_windows=1,
            **({"is_paused": self.paused} if self.paused else {}),
            **({"on_fire": self.on_fire} if self.on_fire else {}),
        )


class TestRunLoop:
    def test_mic_stays_closed_while_nothing_is_speaking(self):
        h = Harness([False] * 5, [])
        h.run()
        assert h.opened == []

    def test_sends_one_barge_and_closes_the_mic(self):
        h = Harness([True] * 10, [(SPEECH, LOUD)] * 10)
        h.run()
        assert h.barges == 1
        assert h.opened[0].closed

    def test_echo_alone_never_barges(self):
        h = Harness([True] * 10 + [False], [(SPEECH, ECHO)] * 10)
        h.run()
        assert h.barges == 0
        assert h.opened[0].closed

    def test_mic_closes_when_speech_ends_naturally(self):
        h = Harness([True, True, False, False], [(NOT_SPEECH, LOUD)] * 20)
        h.run()
        assert h.opened[0].closed

    def test_startup_windows_are_ignored(self):
        h = Harness([True] * 6 + [False], [(SPEECH, LOUD)] * 3 + [(NOT_SPEECH, ECHO)] * 3, warmup=3)
        h.run()
        assert h.barges == 0

    def test_mic_stays_closed_while_paused(self):
        h = Harness([True] * 5, [(SPEECH, LOUD)] * 10, paused=lambda: True)
        h.run()
        assert h.opened == []
        assert h.barges == 0

    def test_open_mic_closes_when_pause_starts(self):
        states = iter([False, False, True, True, True])
        h = Harness([True] * 6, [(NOT_SPEECH, LOUD)] * 20, paused=lambda: next(states, True))
        h.run()
        assert len(h.opened) == 1
        assert h.opened[0].closed
        assert h.barges == 0

    def test_fire_calls_on_fire_once(self):
        calls = []
        h = Harness([True] * 10, [(SPEECH, LOUD)] * 10, on_fire=lambda: calls.append(1))
        h.run()
        assert h.barges == 1
        assert calls == [1]

    def test_each_utterance_starts_with_fresh_vad_state(self):
        h = Harness([True, False, True, False], [])
        h.run()
        assert h.vad.resets == 2


class TestSpeakingQuery:
    def test_missing_socket_means_not_speaking(self, tmp_path):
        assert voice_barge.is_speaking(tmp_path / "absent.sock") is False

    def test_garbage_path_means_not_speaking(self, tmp_path):
        p = tmp_path / "not-a-socket"
        p.write_text("x")
        assert voice_barge.is_speaking(p) is False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += voice_barge.WINDOW_S
        return self.t


class TestPrerollHandoff:
    def _run(self, frames, listen_after_polls, lookback=31):
        polls = {"n": 0}
        clock = Clock()
        got = {"clock": clock}

        def poll_listen():
            polls["n"] += 1
            return 1000.5 if polls["n"] >= listen_after_polls else None

        speaking = [True] * 200 + [False]
        vad = FakeVad()
        caps = []

        def open_capture():
            caps.append(FakeCapture(frames, vad))
            return caps[-1]

        voice_barge.run(
            poll_speaking=lambda: speaking.pop(0) if speaking else None,
            open_capture=open_capture, vad=vad, detector=detector(),
            send_barge=lambda: got.setdefault("barged", list(frames)),
            sleep=lambda _s: None, warmup_windows=0, poll_every_windows=1,
            poll_listen_started=poll_listen,
            preroll_sink=lambda samples, start: got.update(samples=samples, start=start),
            clock=clock, preroll_windows=lookback,
        )
        return got, caps

    def test_audio_before_the_trigger_and_up_to_listening_is_handed_over(self):
        frames = [(SPEECH, LOUD)] * 3 + [(NOT_SPEECH, LOUD)] * 7
        got, caps = self._run(frames, listen_after_polls=4)
        assert len(got["samples"]) == 512 * 3 + 512 * 4
        assert caps[0].closed

    def test_lookback_is_bounded(self):
        frames = [(NOT_SPEECH, LOUD)] * 20 + [(SPEECH, LOUD)] * 3 + [(NOT_SPEECH, LOUD)] * 5
        got, _ = self._run(frames, listen_after_polls=1, lookback=5)
        assert len(got["samples"]) == 512 * (5 + 1)

    def test_start_time_is_derived_from_the_samples_held(self):
        frames = [(SPEECH, LOUD)] * 3 + [(NOT_SPEECH, LOUD)] * 3
        got, _ = self._run(frames, listen_after_polls=1)
        held_s = len(got["samples"]) / voice_barge.SAMPLE_RATE
        assert got["start"] == pytest.approx(got["clock"].t - held_s)

    def test_hands_over_what_it_has_if_listening_never_starts(self):
        frames = [(SPEECH, LOUD)] * 3
        got, caps = self._run(frames, listen_after_polls=10 ** 9)
        assert len(got["samples"]) == 512 * 3
        assert caps[0].closed


class TestPrependPreroll:
    def _file(self, tmp_path, seconds, start):
        path = tmp_path / "preroll.bin"
        n = int(seconds * voice_barge.SAMPLE_RATE)
        voice_barge.write_preroll(path, np.full(n, 0.5, dtype=np.float32), start)
        return path

    def _prepend(self, path, listen_at, audio=None):
        audio = np.ones(100, dtype=np.int16) if audio is None else audio
        return voice_barge.prepend_preroll(audio, listen_at, 24000, path=path, wait_s=0, sleep=lambda _s: None)

    def test_prepends_resampled_audio_before_the_recording(self, tmp_path):
        path = self._file(tmp_path, 1.0, start=100.0)
        out = self._prepend(path, 101.0)
        assert len(out) == 24000 + 100
        assert out.dtype == np.int16
        assert out[0] > 10000 and out[-1] == 1

    def test_trims_overlap_with_the_recording_by_timestamp(self, tmp_path):
        path = self._file(tmp_path, 1.0, start=100.0)
        assert len(self._prepend(path, 100.5)) == 12000 + 100

    def test_never_invents_audio_when_the_file_is_short(self, tmp_path):
        path = self._file(tmp_path, 0.5, start=100.0)
        assert len(self._prepend(path, 102.0)) == 12000 + 100

    def test_removes_the_file_after_use(self, tmp_path):
        path = self._file(tmp_path, 0.5, start=100.0)
        self._prepend(path, 100.5)
        assert not path.exists()

    def test_file_holding_voice_audio_is_private(self, tmp_path):
        path = self._file(tmp_path, 0.1, start=100.0)
        assert path.stat().st_mode & 0o077 == 0

    @pytest.mark.parametrize("listen_at", [None, 99.0, 200.0])
    def test_stale_or_future_audio_is_discarded(self, tmp_path, listen_at):
        path = self._file(tmp_path, 0.5, start=100.0)
        assert len(self._prepend(path, listen_at)) == 100
        assert not path.exists()

    def test_missing_file_returns_the_recording_unchanged(self, tmp_path):
        assert len(self._prepend(tmp_path / "absent", 100.0)) == 100

    def test_corrupt_file_returns_the_recording_unchanged(self, tmp_path):
        path = tmp_path / "bad"
        path.write_bytes(b"xy")
        assert len(self._prepend(path, 100.0)) == 100

    def test_empty_recording_still_returns_the_preroll_as_int16(self, tmp_path):
        path = self._file(tmp_path, 0.5, start=100.0)
        out = self._prepend(path, 100.5, audio=np.array([]))
        assert len(out) == 12000 and out.dtype == np.int16


def test_lookback_covers_the_measured_soft_voice_detection_delay():
    # Soft speech took 2.5 s to detect (2026-11-04); shorter loses those words.
    assert voice_barge.PREROLL_WINDOWS * voice_barge.WINDOW_S >= 2.5


class TestBargeControl:
    def _switch(self, tmp_path, body='echo "$@"'):
        path = tmp_path / "voicemode-switch"
        path.write_text(f"#!/bin/bash\n{body}\n")
        path.chmod(0o755)
        return path

    def test_passes_the_action_to_voicemode_switch(self, tmp_path):
        assert voice_barge.barge_control("off", self._switch(tmp_path)) == "barge off"

    @pytest.mark.parametrize("action", ["ON", " on ", "Status"])
    def test_action_is_forgiving_about_case_and_spaces(self, tmp_path, action):
        assert voice_barge.barge_control(action, self._switch(tmp_path)) == f"barge {action.strip().lower()}"

    @pytest.mark.parametrize("action", ["", "restart", "on; rm -rf /", "--help"])
    def test_rejects_anything_else_without_running_it(self, tmp_path, action):
        marker = tmp_path / "ran"
        switch = self._switch(tmp_path, f'touch "{marker}"')
        assert "must be on, off or status" in voice_barge.barge_control(action, switch)
        assert not marker.exists()

    def test_strips_terminal_colours(self, tmp_path):
        switch = self._switch(tmp_path, r"printf '\033[0;32mok\033[0m Voice barge-in off\n'")
        assert voice_barge.barge_control("off", switch) == "ok Voice barge-in off"

    def test_reports_a_failure_instead_of_claiming_success(self, tmp_path):
        out = voice_barge.barge_control("on", self._switch(tmp_path, "echo no model; exit 3"))
        assert "failed" in out and "no model" in out

    def test_reports_a_missing_switch(self, tmp_path):
        assert "not found" in voice_barge.barge_control("on", tmp_path / "absent")


def test_tool_file_registers_a_voice_barge_mcp_tool():
    src = (MODULE.parent / "voice_barge_tool.py").read_text()
    assert "@mcp.tool()" in src and "async def voice_barge(" in src
