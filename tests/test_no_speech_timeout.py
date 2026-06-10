"""Behavior tests for the queue's no-speech timeout in the recording loop.

Regression for the 2026-06-11 truncation bug: the old LISTEN_CAP was applied
to `listen_duration_max` — a HARD ceiling in record_audio_with_silence_detection
— so with waiters queued the user was cut off mid-sentence at ~8s even while
still speaking. The fix passes the cap as `no_speech_timeout`, which may only
end a recording in which speech never started; active speech is never truncated.

These tests run the REAL recording loop from the installed (patched)
voice_mode.tools.converse with a fake sounddevice stream and a scripted VAD,
so time is simulated (30ms per chunk) and no microphone is needed.
"""
import threading
import time
import types

import numpy as np
import pytest

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


def _recorded_seconds(audio):
    return len(audio) / converse.SAMPLE_RATE


def test_active_speech_is_never_truncated_by_no_speech_timeout(vad_env):
    """THE BUG: user still speaking at the cap must NOT be cut off.

    Speech runs 0..1.5s, timeout is 0.5s. The recording must continue
    through the speech and end via the normal 1s-silence exit (~2.5s),
    not at the 0.5s timeout.
    """
    vad_env["speech_until_s"] = 1.5
    audio, speech_detected = converse.record_audio_with_silence_detection(
        max_duration=10.0,
        disable_silence_detection=False,
        min_duration=0.0,
        vad_aggressiveness=3,
        no_speech_timeout=0.5,
    )
    assert speech_detected is True
    dur = _recorded_seconds(audio)
    assert dur > 2.0, f"recording truncated at {dur:.2f}s — cap hit during speech"
    assert dur < 4.0, f"recording ran too long ({dur:.2f}s)"


def test_silent_user_yields_at_no_speech_timeout(vad_env):
    """No speech at all: recording stops at the timeout, not max_duration."""
    audio, speech_detected = converse.record_audio_with_silence_detection(
        max_duration=10.0,
        disable_silence_detection=False,
        min_duration=0.0,
        vad_aggressiveness=3,
        no_speech_timeout=0.5,
    )
    assert speech_detected is False
    dur = _recorded_seconds(audio)
    assert dur < 1.0, f"silent recording should stop ~0.5s, got {dur:.2f}s"


def test_no_timeout_means_silent_user_waits_full_window(vad_env):
    """Solo conversation (timeout None): unchanged upstream behavior —
    a silent user is listened to for the full max_duration."""
    audio, speech_detected = converse.record_audio_with_silence_detection(
        max_duration=1.2,
        disable_silence_detection=False,
        min_duration=0.0,
        vad_aggressiveness=3,
        no_speech_timeout=None,
    )
    assert speech_detected is False
    dur = _recorded_seconds(audio)
    assert dur >= 1.1, f"solo silent window should run to max, got {dur:.2f}s"
