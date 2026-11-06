"""macOS barge-in: on the built-in speakers the mic goes through macOS voice processing
(echo cancellation); on headphones it is plain capture. No Mac here: CoreAudio and
AVFoundation are faked. What a fake cannot prove (the real tap format, which channel is
processed, pyobjc's buffer access) is in tests/manual/mac_barge_vp_check.md."""
import ctypes
import importlib.util
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pytest

MODULE = Path(__file__).resolve().parent.parent / "patches" / "voice_barge.py"
spec = importlib.util.spec_from_file_location("voice_barge_mac_under_test", MODULE)
vb = importlib.util.module_from_spec(spec)
sys.modules["voice_barge_mac_under_test"] = vb
spec.loader.exec_module(vb)

REFUSAL = ("barge: built-in speakers without echo cancellation; use headphones "
           "(pyobjc-framework-AVFoundation missing)")


def cc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


# --- output_route: CoreAudio through ctypes ---------------------------------------

class FakeCoreAudio:
    """AudioObjectGetPropertyData with a table of (object, selector) -> value or OSStatus."""

    DEVICE = 73

    def __init__(self, transport=None, source=None, default=DEVICE, fail=()):
        self.values = {(1, cc("dOut")): default}
        if transport is not None:
            self.values[(self.DEVICE, cc("tran"))] = cc(transport)
        if source is not None:
            self.values[(self.DEVICE, cc("ssrc"))] = cc(source)
        self.fail = {cc(s) for s in fail}
        self.calls = []

    def AudioObjectGetPropertyData(self, obj, addr, qsize, qdata, size, out):
        obj = obj.value if hasattr(obj, "value") else obj
        a = addr.contents
        self.calls.append((obj, a.mSelector, a.mScope, a.mElement))
        assert size.contents.value == 4
        if a.mSelector in self.fail or (obj, a.mSelector) not in self.values:
            return cc("who?")  # kAudioHardwareUnknownPropertyError
        out.contents.value = self.values[(obj, a.mSelector)]
        return 0


@pytest.mark.parametrize("transport,source,route", [
    ("blue", None, "headphones"),        # Bluetooth (FELIX-MAC: Jabra reports 'blue', no data source)
    ("usb ", None, "headphones"),
    ("bltn", "hdpn", "headphones"),      # built-in, headphone jack
    ("bltn", "ispk", "speakers"),        # built-in speakers
    ("bltn", None, "speakers"),          # built-in, data source unreadable
])
def test_route_mapping(transport, source, route):
    assert vb.output_route(platform="darwin", lib=FakeCoreAudio(transport, source)) == route


def test_unreadable_transport_is_unknown():
    assert vb.output_route(platform="darwin", lib=FakeCoreAudio(None)) == "unknown"


def test_no_default_output_device_is_unknown():
    lib = FakeCoreAudio("bltn", "ispk", fail=("dOut",))
    assert vb.output_route(platform="darwin", lib=lib) == "unknown"
    assert vb.output_route(platform="darwin", lib=FakeCoreAudio("bltn", "ispk", default=0)) == "unknown"


def test_coreaudio_missing_is_unknown(monkeypatch):
    monkeypatch.setattr(vb, "_coreaudio", lambda: None)
    assert vb.output_route(platform="darwin") == "unknown"


def test_not_macos_is_unknown_without_touching_coreaudio(monkeypatch):
    monkeypatch.setattr(vb, "_coreaudio", lambda: pytest.fail("CoreAudio loaded off macOS"))
    for platform in ("linux", "win32"):
        assert vb.output_route(platform=platform) == "unknown"


def test_reads_the_documented_properties_and_scopes():
    lib = FakeCoreAudio("bltn", "ispk")
    vb.output_route(platform="darwin", lib=lib)
    assert lib.calls == [
        (1, cc("dOut"), cc("glob"), 0),                       # system object, default output
        (FakeCoreAudio.DEVICE, cc("tran"), cc("glob"), 0),    # transport type
        (FakeCoreAudio.DEVICE, cc("ssrc"), cc("outp"), 0),    # output data source
    ]


def test_route_check_is_cheap():
    lib = FakeCoreAudio("bltn", "ispk")
    t = time.perf_counter()
    for _ in range(100):
        vb.output_route(platform="darwin", lib=lib)
    assert (time.perf_counter() - t) / 100 < 0.005


# --- the choice per stretch -------------------------------------------------------

@pytest.mark.parametrize("route,echo,vp,choice", [
    ("headphones", "auto", True, "sounddevice"),
    ("headphones", "auto", False, "sounddevice"),
    ("speakers", "auto", True, "voice-processing"),
    ("speakers", "auto", False, "refused"),
    ("unknown", "auto", True, "sounddevice"),
    ("unknown", "auto", False, "sounddevice"),
    ("headphones", "on", True, "voice-processing"),
    ("unknown", "on", True, "voice-processing"),
    ("speakers", "on", True, "voice-processing"),
    ("headphones", "on", False, "refused"),
    ("speakers", "off", True, "sounddevice"),
    ("speakers", "off", False, "sounddevice"),
    ("headphones", "off", True, "sounddevice"),
])
def test_choice_matrix(route, echo, vp, choice):
    assert vb.choose_capture(route, echo, vp) == choice


@pytest.mark.parametrize("raw,mode", [(None, "auto"), ("", "auto"), ("auto", "auto"), ("ON", "on"),
                                      (" off ", "off"), ('"on"', "on"), ("maybe", "auto")])
def test_echo_setting(raw, mode):
    assert vb.echo_mode(raw) == mode


class Opener:
    """open_capture on 'darwin' with the route, VP availability and captures faked."""

    def __init__(self, monkeypatch, route="speakers", vp=True):
        self.route, self.vp = route, vp
        self.made = []
        monkeypatch.setattr(vb, "SoundDeviceCapture", self._maker("sounddevice"))
        monkeypatch.setattr(vb, "VoiceProcessingCapture", self._maker("voice-processing"))
        monkeypatch.setattr(vb, "ParecCapture", self._maker("parec"))
        vb._REFUSALS.reset()

    def _maker(self, kind):
        outer = self

        class Made:
            def __init__(self, *a, **k):
                self.kind, self.args, self.kw = kind, a, k
                outer.made.append(self)

            def close(self):
                pass
        return Made

    def __call__(self, echo="auto", backend="sounddevice", platform="darwin"):
        return vb.open_capture(backend, "", platform=platform, echo=echo,
                               route=lambda: self.route, vp_available=lambda: self.vp)


def test_speakers_with_vp_open_voice_processing_and_say_so_in_the_stretch_line(monkeypatch):
    o = Opener(monkeypatch)
    cap = o()
    assert cap.kind == "voice-processing"
    assert cap.stretch_note == "route=speakers, capture=voice-processing"


def test_headphones_open_plain_capture(monkeypatch):
    o = Opener(monkeypatch, route="headphones")
    cap = o()
    assert cap.kind == "sounddevice" and cap.stretch_note == "route=headphones, capture=sounddevice"


def test_refusal_opens_no_mic_and_logs_once_per_route_change(monkeypatch, caplog):
    o = Opener(monkeypatch, route="speakers", vp=False)
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        assert all(o().refused for _ in range(3))      # three stretches, same route
        o.route = "headphones"
        assert o().kind == "sounddevice"
        o.route = "speakers"
        assert o().refused and o().refused
    assert o.made and all(m.kind == "sounddevice" for m in o.made)
    refusals = [r for r in caplog.records if r.getMessage() == REFUSAL]
    assert len(refusals) == 2 and all(r.levelname == "WARNING" for r in refusals)


def test_echo_off_never_uses_voice_processing(monkeypatch):
    o = Opener(monkeypatch, route="speakers", vp=True)
    assert o(echo="off").kind == "sounddevice"


def test_echo_on_uses_voice_processing_on_headphones(monkeypatch):
    o = Opener(monkeypatch, route="headphones", vp=True)
    assert o(echo="on").kind == "voice-processing"


def test_echo_on_without_avfoundation_refuses_with_its_own_reason(monkeypatch, caplog):
    o = Opener(monkeypatch, route="headphones", vp=False)
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        assert o(echo="on").refused and o(echo="on").refused
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(lines) == 1 and "VOICEMODE_VOICE_BARGE_ECHO=on" in lines[0] and "AVFoundation" in lines[0]


def test_voice_processing_that_fails_to_start_refuses_once_and_never_falls_back(monkeypatch, caplog):
    o = Opener(monkeypatch, route="speakers", vp=True)

    def boom(*a, **k):
        raise RuntimeError("engine would not start")
    monkeypatch.setattr(vb, "VoiceProcessingCapture", boom)
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        assert o().refused and o().refused
    assert o.made == []
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(lines) == 1 and "engine would not start" in lines[0]


@pytest.mark.parametrize("platform,backend,kind", [("linux", "parec", "parec"),
                                                   ("linux", "sounddevice", "sounddevice"),
                                                   ("win32", "sounddevice", "sounddevice")])
def test_other_platforms_are_unchanged(monkeypatch, platform, backend, kind):
    o = Opener(monkeypatch, route="speakers", vp=False)
    o.route = None  # must not be asked
    cap = vb.open_capture(backend, "", platform=platform, echo="on",
                          route=lambda: pytest.fail("route read off macOS"),
                          vp_available=lambda: pytest.fail("AVFoundation probed off macOS"))
    assert cap.kind == kind and not hasattr(cap, "stretch_note")


def test_parec_on_a_mac_is_the_plain_capture(monkeypatch):
    o = Opener(monkeypatch, route="headphones")
    cap = o(backend="parec")
    assert cap.kind == "parec" and cap.stretch_note == "route=headphones, capture=parec"


# --- run(): a refused stretch and the stretch line --------------------------------

class Cap:
    def __init__(self, n=4, note=None):
        self.n, self.closed = n, False
        if note:
            self.stretch_note = note

    def read_window(self):
        if self.n == 0:
            return None
        self.n -= 1
        return np.full(512, 0.001, dtype=np.float32)

    def close(self):
        self.closed = True


class Vad:
    def reset(self):
        pass

    def prob(self, w):
        return 0.0


def _run(speaking, opener):
    timeline = list(speaking)
    vb.run(poll_speaking=lambda: timeline.pop(0) if timeline else None, open_capture=opener,
           vad=Vad(), detector=vb.BargeDetector(-35.0, 3), send_barge=lambda: pytest.fail("fired"),
           sleep=lambda s: None, warmup_windows=0, poll_every_windows=1)


def test_a_refused_stretch_waits_for_the_next_one_without_reasking(caplog):
    opened = []

    def opener():
        opened.append(1)
        return None if len(opened) == 1 else Cap()
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        _run([True, True, True, False, True, False], opener)
    assert len(opened) == 2   # once per stretch, not once per poll
    assert len([r for r in caplog.records if r.getMessage().startswith("stretch:")]) == 1


def test_stretch_line_carries_route_and_capture_when_the_capture_has_them(caplog):
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        _run([True, True, False], lambda: Cap(note="route=speakers, capture=voice-processing"))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stretch:")]
    import re
    assert re.search(r"fired no, first_window \d+ ms, deaf \d+ ms, cold, route=speakers, capture=voice-processing$", line), line


def test_stretch_line_without_them_ends_with_the_timing(caplog):
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        _run([True, True, False], lambda: Cap())
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stretch:")]
    assert "fired no, first_window " in line and line.endswith(", cold")


# --- VoiceProcessingCapture against a fake AVFoundation --------------------------

class FakeVarlist:
    def __init__(self, arr):
        self.arr = np.ascontiguousarray(arr, dtype=np.float32)

    def as_buffer(self, count):
        return memoryview(self.arr[:count].tobytes())


class FakeBuffer:
    def __init__(self, channels: np.ndarray, interleaved=False):
        self._frames = channels.shape[1]
        if interleaved:
            self._data = [FakeVarlist(channels.T.reshape(-1))]
            self._stride = channels.shape[0]
        else:
            self._data = [FakeVarlist(c) for c in channels]
            self._stride = 1

    def frameLength(self):
        return self._frames

    def floatChannelData(self):
        return self._data

    def stride(self):
        return self._stride


class FakeFormat:
    def __init__(self, rate, channels, interleaved=False):
        self.rate, self.channels, self.interleaved = rate, channels, interleaved

    def sampleRate(self):
        return float(self.rate)

    def channelCount(self):
        return self.channels

    def isInterleaved(self):
        return self.interleaved


class FakeInputNode:
    def __init__(self, fmt, vp_ok=True):
        self.fmt, self.vp_ok = fmt, vp_ok
        self.vp = None
        self.ducking = None
        self.tap = None
        self.removed = []

    def setVoiceProcessingEnabled_error_(self, on, err=None):
        self.vp = on
        return (self.vp_ok, None if self.vp_ok else "no VP")

    def setVoiceProcessingOtherAudioDuckingConfiguration_(self, config):
        self.ducking = config

    def outputFormatForBus_(self, bus):
        return self.fmt

    def installTapOnBus_bufferSize_format_block_(self, bus, size, fmt, block):
        self.tap = (bus, size, fmt, block)

    def removeTapOnBus_(self, bus):
        self.removed.append(bus)
        self.tap = None


class FakeEngine:
    def __init__(self, node, start_ok=True):
        self.node, self.start_ok = node, start_ok
        self.running = False
        self.stops = 0

    def inputNode(self):
        return self.node

    def prepare(self):
        pass

    def startAndReturnError_(self, err=None):
        self.running = self.start_ok
        return (self.start_ok, None if self.start_ok else "start failed")

    def stop(self):
        self.running = False
        self.stops += 1


class FakeAVFoundation:
    def __init__(self, rate=48000, channels=9, interleaved=False, vp_ok=True, start_ok=True):
        self.node = FakeInputNode(FakeFormat(rate, channels, interleaved), vp_ok)
        self.engine = FakeEngine(self.node, start_ok)
        av = self

        class _Alloc:
            def init(self):
                return av.engine

        class AVAudioEngine:
            @staticmethod
            def alloc():
                return _Alloc()
        self.AVAudioEngine = AVAudioEngine

    def deliver(self, channels, interleaved=False):
        self.node.tap[3](FakeBuffer(channels, interleaved), None)


def tone(freq, rate, seconds, amp=0.5, phase=0.0):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t + phase)).astype(np.float32)


def nine_channels(ch0, seed=1):
    rng = np.random.default_rng(seed)
    others = rng.uniform(-0.9, 0.9, size=(8, len(ch0))).astype(np.float32)  # loud, unprocessed
    return np.vstack([ch0, others])


def collect(cap):
    out = []
    while True:
        w = cap.read_window()
        if w is None:
            return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)
        assert w.dtype == np.float32 and len(w) == vb.WINDOW_SAMPLES
        out.append(w)


def peak_hz(x, rate=16000):
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[int(np.argmax(spectrum))]


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


@pytest.fixture
def av(monkeypatch):
    def make(**kw):
        fake = FakeAVFoundation(**kw)
        monkeypatch.setitem(sys.modules, "AVFoundation", fake)
        return fake
    return make


def test_turns_voice_processing_on_ducking_at_minimum_and_taps_the_input(av):
    fake = av()
    cap = vb.VoiceProcessingCapture()
    assert fake.node.vp is True
    assert fake.node.ducking == (False, 10)
    assert fake.node.tap is not None and fake.node.tap[0] == 0 and fake.node.tap[2] is fake.node.fmt
    assert fake.engine.running
    cap.close()


@pytest.mark.parametrize("rate", [48000, 44100])
@pytest.mark.parametrize("interleaved", [False, True])
def test_channel_0_resampled_to_16k_windows(av, rate, interleaved):
    fake = av(rate=rate, interleaved=interleaved)
    cap = vb.VoiceProcessingCapture()
    signal = tone(1000, rate, 1.0)
    block = 4800 if rate == 48000 else 4410
    for i in range(0, len(signal), block):
        fake.deliver(nine_channels(signal[i:i + block], seed=i), interleaved=interleaved)
    out = collect(cap)
    assert abs(len(out) - 16000) <= vb.WINDOW_SAMPLES   # one second in, one second out
    steady = out[1000:]
    assert abs(peak_hz(steady) - 1000) < 20
    assert rms(steady) == pytest.approx(0.5 / np.sqrt(2), rel=0.05)   # the other 8 channels left out
    cap.close()


@pytest.mark.parametrize("rate", [48000, 44100])
def test_above_8k_is_filtered_not_folded_back(av, rate):
    # [::3] alone would fold a 12 kHz tone to 4 kHz at full level.
    fake = av(rate=rate)
    cap = vb.VoiceProcessingCapture()
    signal = tone(12000, rate, 1.0)
    for i in range(0, len(signal), 4096):
        fake.deliver(nine_channels(signal[i:i + 4096]))
    out = collect(cap)[1000:]
    assert 20 * np.log10(rms(out) / (0.5 / np.sqrt(2)) + 1e-12) < -30
    cap.close()


def test_uneven_buffers_are_continuous(av):
    fake = av(rate=44100)
    cap = vb.VoiceProcessingCapture()
    signal = tone(440, 44100, 1.0)
    i = 0
    for size in [1000, 4410, 17, 2048, 333] * 20:
        if i >= len(signal):
            break
        fake.deliver(nine_channels(signal[i:i + size]))
        i += size
    out = collect(cap)[1000:]
    # A seam (dropped or repeated samples) shows up as energy away from 440 Hz.
    spectrum = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    freqs = np.fft.rfftfreq(len(out), 1 / 16000)
    off = spectrum[np.abs(freqs - 440) > 60]
    assert 20 * np.log10(off.max() / spectrum.max()) < -40
    cap.close()


def test_nothing_for_200_ms_is_none(av):
    av()
    cap = vb.VoiceProcessingCapture()
    t = time.monotonic()
    assert cap.read_window() is None
    assert 0.15 < time.monotonic() - t < 1.0
    cap.close()


def test_close_stops_the_engine_and_removes_the_tap(av):
    fake = av()
    vb.VoiceProcessingCapture().close()
    assert not fake.engine.running and fake.engine.stops == 1
    assert fake.node.removed == [0] and fake.node.tap is None


def test_voice_processing_refused_by_the_system_raises_and_leaves_nothing_running(av):
    fake = av(vp_ok=False)
    with pytest.raises(RuntimeError):
        vb.VoiceProcessingCapture()
    assert not fake.engine.running and fake.node.tap is None


def test_engine_that_will_not_start_raises_and_removes_the_tap(av):
    fake = av(start_ok=False)
    with pytest.raises(RuntimeError):
        vb.VoiceProcessingCapture()
    assert fake.node.tap is None


def test_channel_choice_is_a_named_constant():
    assert vb.VP_CHANNEL == 0
    src = MODULE.read_text()
    at = src.index("VP_CHANNEL =")
    assert "UNVERIFIED" in src[max(0, at - 600):at]   # the comment right above it


def test_voice_processing_available_imports_avfoundation_once(monkeypatch):
    monkeypatch.setitem(sys.modules, "AVFoundation", FakeAVFoundation())
    vb._VP_AVAILABLE.clear()
    assert vb.voice_processing_available() is True
    monkeypatch.setitem(sys.modules, "AVFoundation", None)   # import now fails, but cached
    assert vb.voice_processing_available() is True
    vb._VP_AVAILABLE.clear()
    assert vb.voice_processing_available() is False
    vb._VP_AVAILABLE.clear()


# --- status: a second line on macOS only ------------------------------------------

def _status(monkeypatch, capsys, platform, route="speakers", vp=True, echo=None, state=True,
            backend="sounddevice"):
    monkeypatch.setattr(vb, "barge_state", lambda home=None: state)
    monkeypatch.setattr(vb, "capture_backend", lambda: backend)
    monkeypatch.setattr(vb, "_platform", lambda: platform)
    monkeypatch.setattr(vb, "output_route", lambda platform=None, lib=None: route)
    monkeypatch.setattr(vb, "voice_processing_available", lambda: vp)
    monkeypatch.setattr(vb, "trigger_settings",
                        lambda: {} if echo is None else {"VOICEMODE_VOICE_BARGE_ECHO": echo})
    monkeypatch.setattr(sys, "argv", ["voice_barge", "status"])
    assert vb.main() == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("route,vp,echo,second", [
    ("speakers", True, None, "route: speakers, capture: voice-processing"),
    ("speakers", False, None, "route: speakers, capture: refused"),
    ("headphones", True, None, "route: headphones, capture: sounddevice"),
    ("speakers", True, "off", "route: speakers, capture: sounddevice"),
])
def test_status_on_macos_has_a_route_line(monkeypatch, capsys, route, vp, echo, second):
    assert _status(monkeypatch, capsys, "darwin", route, vp, echo) == f"on\n{second}\n"


def test_status_off_macos_is_one_line(monkeypatch, capsys):
    assert _status(monkeypatch, capsys, "linux") == "on\n"


def test_status_cannot_run_has_no_route_line(monkeypatch, capsys):
    out = _status(monkeypatch, capsys, "darwin", state=None)
    assert out.startswith("cannot run: ") and out.count("\n") == 1


def test_pill_probe_reads_only_the_first_line():
    # barge_available's probe of the trigger's python: the second line must not change its answer.
    src = (MODULE.parent / "voice_pill_state.py").read_text()
    probe = src[src.index("def _probe_barge"):][:900]
    assert "line[0]" in probe


def test_pyobjc_that_wants_the_error_argument_gets_none(av):
    # The spike left the NSError** out; some pyobjc versions insist on it as None.
    fake = av()
    seen = []

    def strict(on, err):
        seen.append((on, err))
        return (True, None)
    fake.node.setVoiceProcessingEnabled_error_ = strict
    vb.VoiceProcessingCapture().close()
    assert seen[0] == (True, None)



# --- fix round 1 ------------------------------------------------------------------

def test_status_and_stretch_agree_on_parec_on_a_mac(monkeypatch, capsys):
    # M2: a Mac with parec labels plain capture "parec" in both places.
    assert _status(monkeypatch, capsys, "darwin", "headphones", backend="parec") == "on\nroute: headphones, capture: parec\n"


@pytest.fixture(autouse=True)
def vp_state(monkeypatch, tmp_path):
    """Every test: the recorded voice-processing failure lives in tmp, never ~/.voicemode."""
    path = tmp_path / "barge_vp_failed.json"
    monkeypatch.setattr(vb, "_vp_failure_path", lambda: path)
    vb._VP_FAILED.clear()
    yield path
    vb._VP_FAILED.clear()


def test_failed_voice_processing_is_remembered_for_the_route_and_not_retried(monkeypatch, vp_state):
    # M3: no retry at every stretch on the same route; retried after a route change.
    o = Opener(monkeypatch, route="speakers", vp=True)
    tries = []

    def boom(*a, **k):
        tries.append(1)
        raise RuntimeError("engine would not start")
    monkeypatch.setattr(vb, "VoiceProcessingCapture", boom)
    first, second = o(), o()
    assert len(tries) == 1
    for cap in (first, second):
        assert cap.refused
        assert cap.stretch_note == "route=speakers, capture=refused (echo cancellation failed to start)"
    o.route = "headphones"
    o()
    o.route = "speakers"
    o()
    assert len(tries) == 2


def test_status_says_when_echo_cancellation_failed_to_start(monkeypatch, capsys, vp_state):
    # M3: the trigger records the failure in a file; the status process reads it.
    o = Opener(monkeypatch, route="speakers", vp=True)
    monkeypatch.setattr(vb, "VoiceProcessingCapture", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    o()
    assert vp_state.exists()
    out = _status(monkeypatch, capsys, "darwin", "speakers", True)
    assert out == "on\nroute: speakers, capture: refused (echo cancellation failed to start)\n"
    # on another route the old failure does not apply
    out = _status(monkeypatch, capsys, "darwin", "headphones", True)
    assert out == "on\nroute: headphones, capture: sounddevice\n"


def test_a_started_voice_processing_clears_the_recorded_failure(monkeypatch, vp_state):
    vb._record_vp_failure("speakers", "old")
    o = Opener(monkeypatch, route="speakers", vp=True)
    vb._VP_FAILED.clear()   # a new trigger process: memory empty, file left over
    assert o().kind == "voice-processing"
    assert not vp_state.exists()


def test_missing_avfoundation_refusal_says_why_on_the_stretch(monkeypatch):
    o = Opener(monkeypatch, route="speakers", vp=False)
    cap = o()
    assert cap.refused
    assert cap.stretch_note == "route=speakers, capture=refused (pyobjc-framework-AVFoundation missing)"


def test_every_refused_stretch_gets_a_stretch_line(caplog):
    # M3: one warning per route change is not enough to read the stretches by.
    def opener():
        return vb.RefusedCapture("route=speakers, capture=refused (echo cancellation failed to start)")
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        _run([True, True, False, True, False], opener)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stretch:")]
    assert len(lines) == 2
    assert all(l.endswith("windows 0, max n/a, p90 n/a, fired no, "
                          "route=speakers, capture=refused (echo cancellation failed to start)")
               for l in lines), lines


def test_first_window_is_timed_from_before_the_mic_opens(caplog):
    clock = iter([0.0, 0.25, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])

    class Slow(Cap):
        def __init__(self):
            super().__init__(n=2, note="route=speakers, capture=voice-processing")
            self.first = True

        def read_window(self):
            if self.first:
                self.first = False
                return None   # voice processing still starting
            return super().read_window()
    timeline = [True, True, True, False]
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        vb.run(poll_speaking=lambda: timeline.pop(0) if timeline else None, open_capture=Slow,
               vad=Vad(), detector=vb.BargeDetector(-35.0, 3), send_barge=lambda: None,
               sleep=lambda s: None, warmup_windows=0, poll_every_windows=1,
               stretch_clock=lambda: next(clock))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stretch:")]
    assert ", first_window 250 ms, deaf 400 ms, cold, " in line, line


def test_no_window_at_all_says_none(caplog):
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        _run([True, True, False], lambda: Cap(n=0, note="route=speakers, capture=voice-processing"))
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("stretch:")]
    assert ", first_window none, deaf none, cold, route=" in line


def test_a_failing_tap_logs_one_traceback_then_a_count_at_close(av, caplog):
    # M4
    fake = av()
    cap = vb.VoiceProcessingCapture()
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        for _ in range(5):
            fake.node.tap[3](object(), None)   # not a buffer: every call fails
        cap.close()
    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == 1
    assert any("tap failed 5 times" in r.getMessage() for r in caplog.records)


def test_close_turns_voice_processing_off_after_stopping(av):
    # M5
    fake = av()
    vb.VoiceProcessingCapture().close()
    assert fake.node.vp is False and not fake.engine.running


def test_close_survives_voice_processing_off_failing(av):
    fake = av()
    cap = vb.VoiceProcessingCapture()

    def boom(*a):
        raise RuntimeError("no")
    fake.node.setVoiceProcessingEnabled_error_ = boom
    cap.close()
    assert fake.node.removed == [0]


@pytest.mark.parametrize("error", [AttributeError, TypeError])
def test_ducking_that_cannot_be_set_logs_once_and_keeps_voice_processing(av, caplog, error):
    fake = av()

    def no(config):
        raise error("struct conversion")
    fake.node.setVoiceProcessingOtherAudioDuckingConfiguration_ = no
    vb._DUCKING_LOGGED.clear()
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        for _ in range(3):
            vb.VoiceProcessingCapture().close()
    assert len([r for r in caplog.records if "ducking not settable" in r.getMessage()]) == 1
    assert fake.engine.stops == 3   # it started every time


def test_channel_probe_logs_rms_per_channel_at_close(av, caplog):
    # VOICEMODE_VOICE_BARGE_VP_CHANNEL_PROBE=1: settle VP_CHANNEL on a real Mac.
    fake = av(channels=3)
    cap = vb.VoiceProcessingCapture(probe=True)
    chans = np.vstack([np.full(4800, 0.001, np.float32), np.full(4800, 0.1, np.float32),
                       np.full(4800, 1.0, np.float32)])
    fake.deliver(chans)
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        cap.close()
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("vp channel probe:")]
    assert "4800 frames" in line and "0 -60.0 dBFS" in line and "1 -20.0 dBFS" in line and "2 0.0 dBFS" in line


def test_no_probe_line_by_default(av, caplog):
    fake = av()
    cap = vb.VoiceProcessingCapture()
    fake.deliver(nine_channels(np.zeros(4800, np.float32)))
    with caplog.at_level(logging.INFO, logger="voicemode.voice_barge"):
        cap.close()
    assert not [r for r in caplog.records if "probe" in r.getMessage()]


def test_open_capture_passes_the_probe_setting(monkeypatch):
    o = Opener(monkeypatch, route="speakers", vp=True)
    cap = vb.open_capture("sounddevice", "", platform="darwin", echo="auto", route=lambda: "speakers",
                          vp_available=lambda: True, vp_probe=True)
    assert cap.kind == "voice-processing"
    made = o.made[-1]
    assert made.kw == {"probe": True}


# --- voicemode-switch barge status on a Mac (M1) ----------------------------------

SWITCH = MODULE.parent.parent / "voicemode-switch"


def _switch_status(tmp_path, uname):
    import os
    import subprocess
    root, home, bin_dir, scratch = (tmp_path / n for n in ("app", "home", "bin", "scratch"))
    for d in (root / ".venv/bin", home / ".voicemode", bin_dir, scratch):
        d.mkdir(parents=True, exist_ok=True)
    script = root / "voicemode-switch"
    script.write_text(SWITCH.read_text().replace("/tmp/", f"{scratch}/"))
    script.chmod(0o755)
    py_log = scratch / "python.log"
    py = root / ".venv/bin/python"
    py.write_text("#!/bin/bash\n"
                  f'echo "$@" >> "{py_log}"\n'
                  'if [ "$*" = "-m voice_mode.voice_barge status" ]; then\n'
                  '  echo on; echo "route: speakers, capture: voice-processing"; fi\n')
    py.chmod(0o755)
    (bin_dir / "uname").write_text(f"#!/bin/bash\necho {uname}\n")
    (bin_dir / "uname").chmod(0o755)
    (home / ".voicemode/voicemode.env").write_text("VOICEMODE_VOICE_BARGE=true\n")
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    done = subprocess.run([str(script), "barge", "status"], env=env, capture_output=True,
                          text=True, timeout=30)
    return done, (py_log.read_text() if py_log.exists() else "")


def test_switch_barge_status_on_a_mac_shows_the_route_line(tmp_path):
    done, calls = _switch_status(tmp_path, "Darwin")
    assert done.returncode == 0, done.stderr
    assert "route: speakers, capture: voice-processing" in done.stdout
    assert done.stdout.count("route:") == 1 and "\non\n" not in "\n" + done.stdout   # only line 2


def test_switch_barge_status_elsewhere_has_no_route_line(tmp_path):
    done, calls = _switch_status(tmp_path, "Linux")
    assert done.returncode == 0, done.stderr
    assert "route:" not in done.stdout and "voice_barge status" not in calls
