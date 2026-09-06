"""Behavior tests for the listen-overrun fix in the recording loop.

Regression for the 2026-06-11 mid-speech truncation bug, layer 2: upstream's
`max_duration` (= listen_duration_max, 60-120s as chosen by the calling agent)
is a HARD ceiling in record_audio_with_silence_detection's while loop, so a
user still talking at the cap was cut off mid-sentence (recordings ended at
exactly 60.0s/120.0s of samples; transcripts ended mid-word). Same bug class
as the queue LISTEN_CAP truncation fixed the same day, one layer deeper —
and it bites SOLO sessions too (the queue was idle: waiting=0).

The fix: once speech has started, the window extends past max_duration until
the normal silence exit, bounded by max_duration + VOICEMODE_LISTEN_OVERRUN
(safety ceiling for a VAD stuck reporting speech, e.g. constant background
noise). A silent window is still capped at max_duration, and overrun 0
restores the upstream hard cap.

These tests run the REAL recording loop from the installed (patched)
voice_mode.tools.converse with a fake sounddevice stream and a scripted VAD,
so time is simulated (30ms per chunk) and no microphone is needed.
"""
import threading
import time
import types

import numpy as np
import pytest


# These tests drive the REAL recording loop out of the INSTALLED voice_mode, so
# they only mean anything against a venv that patches/apply.sh has processed.
# Run against an unpatched install they fail confusingly (the loop still hard-
# caps at max_duration, which is the upstream bug) -- so say so instead.
def _installed_converse_is_patched() -> bool:
    try:
        import voice_mode.tools.converse as c
        from pathlib import Path as _P
        return "voicemode-local listen overrun" in _P(c.__file__).read_text()
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _installed_converse_is_patched(),
    reason="installed voice_mode is not patched -- run patches/apply.sh on this venv first",
)

converse = pytest.importorskip("voice_mode.tools.converse")

CHUNK_MS = 30  # converse.VAD_CHUNK_DURATION_MS


class FakeVad:
    """Scripted VAD: speech_until_s of 'speech', silence afterwards."""

    def __init__(self, aggressiveness):
        self.calls = 0
        self.speech_until_s = 0.0

    def is_speech(self, chunk_bytes, sample_rate):
        t = self.calls * CHUNK_MS / 1000.0
        self.calls += 1
        return t < self.speech_until_s


class FakeInputStream:
    """Feeds 30ms int16 chunks to the callback from a producer thread."""

    def __init__(self, samplerate, channels, dtype, callback, blocksize):
        self.callback = callback
        self.blocksize = blocksize
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._produce, daemon=True)

    def _produce(self):
        chunk = np.zeros((self.blocksize, 1), dtype=np.int16)
        while not self._stop.is_set():
            self.callback(chunk, self.blocksize, None, None)
            time.sleep(0.001)  # ~30x real-time; keeps queue bounded

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2)
        return False


@pytest.fixture
def vad_env(monkeypatch):
    """Patch converse's audio/VAD environment.

    Yields a config dict; set cfg["speech_until_s"] BEFORE calling the
    recording function to script how long the fake user 'speaks'.
    """
    cfg = {"speech_until_s": 0.0}

    def vad_factory(aggressiveness):
        v = FakeVad(aggressiveness)
        v.speech_until_s = cfg["speech_until_s"]
        return v

    fake_sd = types.SimpleNamespace(
        InputStream=FakeInputStream,
        PortAudioError=RuntimeError,
    )
    monkeypatch.setattr(converse, "sd", fake_sd)
    monkeypatch.setattr(converse, "webrtcvad",
                        types.SimpleNamespace(Vad=vad_factory))
    monkeypatch.setattr(converse, "VAD_AVAILABLE", True)
    monkeypatch.setattr(converse, "DISABLE_SILENCE_DETECTION", False)
    monkeypatch.setattr(converse, "VAD_DEBUG", False)
    monkeypatch.setattr(converse, "DEBUG", False)
    monkeypatch.setattr(converse, "SILENCE_THRESHOLD_MS", 1000)
    monkeypatch.setattr(converse, "MIN_RECORDING_DURATION", 0.0)
    return cfg


def _record(max_duration):
    audio, speech_detected = converse.record_audio_with_silence_detection(
        max_duration=max_duration,
        disable_silence_detection=False,
        min_duration=0.0,
        vad_aggressiveness=3,
    )
    return len(audio) / converse.SAMPLE_RATE, speech_detected


def test_active_speech_extends_past_max_duration(vad_env):
    """THE BUG: user still speaking at max_duration must NOT be cut off.

    Speech runs 0..2.0s, max_duration is 1.0s. The recording must continue
    through the speech and end via the normal 1s-silence exit (~3.0s),
    not at the 1.0s hard cap.
    """
    vad_env["speech_until_s"] = 2.0
    dur, speech_detected = _record(max_duration=1.0)
    assert speech_detected is True
    assert dur > 2.0, f"recording truncated at {dur:.2f}s — hard cap hit during speech"
    assert dur < 4.5, f"recording ran too long ({dur:.2f}s)"


def test_silent_window_still_capped_at_max_duration(vad_env):
    """No speech at all: overrun must NOT extend the silent waiting window —
    it still stops at max_duration (unchanged upstream behavior)."""
    dur, speech_detected = _record(max_duration=1.0)
    assert speech_detected is False
    assert dur < 1.5, f"silent window should stop at max_duration, got {dur:.2f}s"


def test_overrun_safety_ceiling(vad_env, monkeypatch):
    """A VAD stuck on 'speech' (e.g. constant background noise) must stop at
    max_duration + VOICEMODE_LISTEN_OVERRUN, not record forever."""
    monkeypatch.setenv("VOICEMODE_LISTEN_OVERRUN", "0.5")
    vad_env["speech_until_s"] = 100.0
    dur, speech_detected = _record(max_duration=1.0)
    assert speech_detected is True
    assert 1.3 < dur < 2.2, f"expected stop at ~1.5s ceiling, got {dur:.2f}s"


def test_overrun_zero_restores_upstream_hard_cap(vad_env, monkeypatch):
    monkeypatch.setenv("VOICEMODE_LISTEN_OVERRUN", "0")
    vad_env["speech_until_s"] = 100.0
    dur, speech_detected = _record(max_duration=1.0)
    assert speech_detected is True
    assert dur < 1.4, f"overrun=0 should hard-cap at 1.0s, got {dur:.2f}s"
