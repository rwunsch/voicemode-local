"""patches/patch_audio_recover.py: a recording whose PortAudio stream fails to start
("Error starting stream: Wait timed out [PaErrorCode -9987]", seen 2026-11-18 22:24:53) is
re-initialised and retried ONCE with silence detection, instead of falling straight to a
fixed-length recording with no silence detection; the fixed-length fallback ends when the
caller's stop_event is set and can be capped with VOICEMODE_FALLBACK_RECORD_MAX_S.

Errors upstream already handles, and everything with VOICEMODE_AUDIO_RECOVER=false, behave
exactly as upstream's code. The fixture's except block is upstream 8.12.0's, verbatim.
"""
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
PATCHES = REPO / "patches"
PATCHER = PATCHES / "patch_audio_recover.py"
MARKER = "voicemode-local audio recover"
TAG = "(audio recover)"
RETRY_LINE = "Audio start timed out - re-initialising audio and retrying once"

# Upstream 8.12.0's shape: record_audio's blocking sd.rec/sd.wait, and
# record_audio_with_silence_detection's except blocks verbatim (converse.py:1619-1685).
CONVERSE = '''\
import os
import threading
import time
from typing import Optional

import numpy as np

SAMPLE_RATE = 1000
CHANNELS = 1
sd = None
LOG = []


class _Logger:
    def _log(self, msg, *args, **kwargs):
        LOG.append(str(msg))
    info = warning = error = debug = _log


logger = _Logger()


def VAD_INIT():
    return None


def record_audio(duration: float) -> np.ndarray:
    """Record audio from microphone"""
    logger.info(f"Recording audio for {duration}s...")
    try:
        recording = sd.rec(int(duration * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=CHANNELS, dtype=np.int16)
        sd.wait()
        return recording.flatten()
    except Exception as e:
        logger.error(f"Recording failed: {e}")
        return np.array([])


def record_audio_with_silence_detection(max_duration: float, disable_silence_detection: bool = False, min_duration: float = 0.0, vad_aggressiveness: Optional[int] = None, stop_event: Optional[threading.Event] = None):
    """Record audio."""
    try:
        VAD_INIT()
        import sys
        original_stdin = sys.stdin
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS):
                return (np.ones(3, dtype=np.int16), True)

        except Exception as e:
            logger.error(f"Recording with VAD failed: {e}")

            # Import here to avoid circular imports
            from voice_mode.utils.audio_diagnostics import get_audio_error_help

            # Check if this is a device error that might be recoverable
            error_str = str(e).lower()
            if any(err in error_str for err in ['device unavailable', 'device disconnected',
                                                 'invalid device', 'unanticipated host error',
                                                 'portaudio error']):
                logger.info("Audio device error detected - attempting to reinitialize audio system")

                # Try to reinitialize sounddevice
                try:
                    # Get current default device info before reinit
                    try:
                        old_device = sd.query_devices(kind='input')
                        old_device_name = old_device.get('name', 'Unknown')
                    except:
                        old_device_name = 'Previous device'

                    sd._terminate()
                    sd._initialize()

                    # Get new default device info
                    try:
                        new_device = sd.query_devices(kind='input')
                        new_device_name = new_device.get('name', 'Unknown')
                        logger.info(f"Audio system reinitialized - switched from '{old_device_name}' to '{new_device_name}'")
                    except:
                        logger.info("Audio system reinitialized - retrying with new default device")

                    # Wait a moment for the system to stabilize
                    import time as time_module
                    time_module.sleep(0.5)

                    # Try recording again with the new device (recursive call in sync context)
                    logger.info("Retrying recording with new audio device...")
                    return record_audio_with_silence_detection(max_duration, disable_silence_detection, min_duration, vad_aggressiveness, stop_event)

                except Exception as reinit_error:
                    logger.error(f"Failed to reinitialize audio: {reinit_error}")
                    # Fall through to normal error handling

            # Get helpful error message
            help_message = get_audio_error_help(e)
            logger.error(f"\\n{help_message}")

            logger.info("Falling back to fixed duration recording")
            # For fallback, assume speech is present since we can't detect
            return (record_audio(max_duration), True)

        finally:
            # Restore stdio
            if sys.stdin != original_stdin:
                sys.stdin = original_stdin

    except Exception as e:
        logger.error(f"VAD initialization failed: {e}")
        logger.info("Falling back to fixed duration recording")
        # For fallback, assume speech is present since we can't detect
        return (record_audio(max_duration), True)


def unrelated():
    return record_audio(0.01)
'''
# Upstream's line ends in a space after the comma; editors strip it, so it is put back here.
CONVERSE = CONVERSE.replace("'device disconnected',\n", "'device disconnected', \n")
assert CONVERSE.count("'device disconnected', \n") == 1

# patch_voice_pill's shapes: record_audio polls the stream (Pause closes it) and upstream's
# recording function becomes ..._inner behind a wrapper.
PILL = CONVERSE.replace(
    "        sd.wait()\n        return recording.flatten()\n",
    "        while sd.get_stream().active:\n"
    "            time.sleep(0.05)\n"
    "        sd.wait()  # closes the finished stream\n"
    "        return recording.flatten()\n",
).replace(
    "def record_audio_with_silence_detection(max_duration",
    "def record_audio_with_silence_detection(max_duration: float, disable_silence_detection: bool = False, "
    "min_duration: float = 0.0, vad_aggressiveness: Optional[int] = None, stop_event: Optional[threading.Event] = None):\n"
    "    return record_audio_with_silence_detection_inner(max_duration, disable_silence_detection, "
    "min_duration, vad_aggressiveness, stop_event)\n\n\n"
    "def record_audio_with_silence_detection_inner(max_duration",
)
assert PILL.count("def record_audio_with_silence_detection_inner(") == 1 and "get_stream().active" in PILL


class PortAudioError(Exception):
    pass


WAIT_TIMEOUT = PortAudioError("Error starting stream: Wait timed out [PaErrorCode -9987]")
HOST_ERROR = PortAudioError("Error starting stream: Unanticipated host error [PaErrorCode -9999]")


class FakeStream:
    """sd.rec's stream: finishes after its duration in real time, or when stopped."""

    def __init__(self, seconds):
        self.finished = threading.Event()
        self.closed = False
        self.stops = 0
        self._timer = threading.Timer(seconds, self.finished.set)
        self._timer.daemon = True
        self._timer.start()

    @property
    def active(self):
        return not self.finished.is_set()

    def stop(self, ignore_errors=True):
        self.stops += 1
        self.finished.set()

    def close(self, ignore_errors=True):
        self.closed = True
        self._timer.cancel()


class FakeSD:
    """The parts of sounddevice converse uses. start_errors: one entry per InputStream
    open, an exception to raise or None to open normally; past the end, opens succeed."""

    def __init__(self, start_errors):
        self.start_errors = list(start_errors)
        self.opens = self.terminates = self.initializes = 0
        self.recs = []
        self.streams = []

    def InputStream(self, **kwargs):
        self.opens += 1
        err = self.start_errors.pop(0) if self.start_errors else None
        if err is not None:
            raise err
        return contextlib.nullcontext()

    def _terminate(self):
        self.terminates += 1

    def _initialize(self):
        self.initializes += 1

    def query_devices(self, kind=None):
        return {"name": "Fake mic"}

    def rec(self, frames, samplerate, channels, dtype):
        self.recs.append(frames / samplerate)
        self.streams.append(FakeStream(frames / samplerate))
        return np.zeros((frames, channels), dtype=np.int16)

    def wait(self):
        stream = self.streams[-1]
        stream.finished.wait()
        stream.close()

    def get_stream(self):
        if not self.streams:
            raise RuntimeError("play()/rec()/playrec() was not called yet")
        return self.streams[-1]


def _run(target):
    return subprocess.run([sys.executable, str(PATCHER), str(target)], capture_output=True,
                          text=True, env=dict(os.environ, PYTHONUTF8="1"))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VOICEMODE_AUDIO_RECOVER", raising=False)
    monkeypatch.delenv("VOICEMODE_FALLBACK_RECORD_MAX_S", raising=False)
    diag = type(sys)("voice_mode.utils.audio_diagnostics")
    diag.get_audio_error_help = lambda e: "help"
    pkg, utils = type(sys)("voice_mode"), type(sys)("voice_mode.utils")
    pkg.__path__, utils.__path__ = [], []
    monkeypatch.setitem(sys.modules, "voice_mode", pkg)
    monkeypatch.setitem(sys.modules, "voice_mode.utils", utils)
    monkeypatch.setitem(sys.modules, "voice_mode.utils.audio_diagnostics", diag)


def _load(path, fake):
    spec = importlib.util.spec_from_file_location("audio_recover_" + uuid.uuid4().hex[:8], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.sd = fake
    return module


def _write(tmp_path, text, name="converse.py"):
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return target


@pytest.fixture(params=["pristine", "pill"])
def shape(request):
    return CONVERSE if request.param == "pristine" else PILL


@pytest.fixture()
def patched(tmp_path, shape):
    target = _write(tmp_path, shape)
    r = _run(target)
    assert r.returncode == 0, r.stdout + r.stderr
    return target


def _plain(tmp_path, shape, fake):
    return _load(_write(tmp_path, shape, "plain.py"), fake)


# -- the retry ----------------------------------------------------------------------------

def test_wait_timeout_reinitialises_once_and_retries_with_silence_detection(patched):
    fake = FakeSD([WAIT_TIMEOUT, None])
    m = _load(patched, fake)
    audio, speech = m.record_audio_with_silence_detection(5)
    assert speech is True and list(audio) == [1, 1, 1]          # the VAD recording's result
    assert (fake.opens, fake.terminates, fake.initializes) == (2, 1, 1)
    assert fake.recs == []                                       # no fixed-length fallback
    assert RETRY_LINE in m.LOG
    assert "Falling back to fixed duration recording" not in m.LOG


@pytest.mark.parametrize("message", ["Wait timed out [PaErrorCode -9987]",
                                     "something [PaErrorCode -9987]",
                                     "Error starting stream: Device busy"])
def test_each_start_failure_marker_is_recoverable(patched, message):
    fake = FakeSD([PortAudioError(message), None])
    m = _load(patched, fake)
    assert m.record_audio_with_silence_detection(5)[1] is True
    assert (fake.opens, fake.terminates) == (2, 1) and fake.recs == []


def test_a_second_failure_falls_back_and_never_loops(patched):
    fake = FakeSD([WAIT_TIMEOUT] * 5)
    m = _load(patched, fake)
    audio, speech = m.record_audio_with_silence_detection(0.2)
    assert (fake.opens, fake.terminates, fake.initializes) == (2, 1, 1)
    assert fake.recs == [0.2] and speech is True and len(audio) == 200
    assert m.LOG.count(RETRY_LINE) == 1
    assert m.LOG.count("Falling back to fixed duration recording") == 1


def test_the_next_recording_may_retry_again(patched):
    """The bound is per recording call, not per process."""
    fake = FakeSD([WAIT_TIMEOUT, WAIT_TIMEOUT, WAIT_TIMEOUT, None])
    m = _load(patched, fake)
    m.record_audio_with_silence_detection(0.1)      # fails twice, falls back
    assert m.record_audio_with_silence_detection(0.1)[0].tolist() == [1, 1, 1]
    assert fake.terminates == 2 and m.LOG.count(RETRY_LINE) == 2


def _same_as_upstream(tmp_path, shape, patched, errors, duration=0.2):
    a, b = FakeSD(list(errors)), FakeSD(list(errors))
    plain, ours = _plain(tmp_path, shape, a), _load(patched, b)
    ra, rb = plain.record_audio_with_silence_detection(duration), ours.record_audio_with_silence_detection(duration)
    assert ra[1] == rb[1] and ra[0].tolist() == rb[0].tolist()
    assert (a.opens, a.terminates, a.initializes, a.recs) == (b.opens, b.terminates, b.initializes, b.recs)
    assert plain.LOG == ours.LOG
    return ours


def test_a_non_recoverable_error_is_untouched(tmp_path, shape, patched):
    ours = _same_as_upstream(tmp_path, shape, patched, [RuntimeError("Invalid number of channels")])
    assert RETRY_LINE not in ours.LOG


@pytest.mark.parametrize("errors", [
    [HOST_ERROR, None],                                   # upstream's list, even with "error starting stream"
    [PortAudioError("Device unavailable [PaErrorCode -9985]"), None],
    [PortAudioError("PortAudio error"), PortAudioError("PortAudio error"), None],
])
def test_errors_upstream_already_handles_behave_as_upstream(tmp_path, shape, patched, errors):
    ours = _same_as_upstream(tmp_path, shape, patched, errors)
    assert RETRY_LINE not in ours.LOG


@pytest.mark.parametrize("value", ["false", "0", "off", "no", "FALSE"])
def test_switch_off_is_upstream(tmp_path, shape, patched, monkeypatch, value):
    monkeypatch.setenv("VOICEMODE_AUDIO_RECOVER", value)
    ours = _same_as_upstream(tmp_path, shape, patched, [WAIT_TIMEOUT, None])
    assert RETRY_LINE not in ours.LOG


def test_reinit_failure_falls_back(patched):
    fake = FakeSD([WAIT_TIMEOUT, None])
    fake._terminate = lambda: (_ for _ in ()).throw(RuntimeError("terminate failed"))
    m = _load(patched, fake)
    m.record_audio_with_silence_detection(0.1)
    assert fake.recs == [0.1] and fake.opens == 1
    assert "Failed to reinitialize audio: terminate failed" in m.LOG


# -- the fixed-length fallback ------------------------------------------------------------

def _record_in_thread(m, duration, stop_event):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "result", m.record_audio_with_silence_detection(duration, stop_event=stop_event)), daemon=True)
    started = time.monotonic()
    t.start()
    return t, out, started


@pytest.mark.parametrize("vad_init_fails", [False, True], ids=["vad-failed", "vad-init-failed"])
def test_stop_event_ends_the_fallback_quickly(patched, vad_init_fails):
    fake = FakeSD([RuntimeError("no start")])
    m = _load(patched, fake)
    if vad_init_fails:
        def boom():
            raise RuntimeError("vad init")
        m.VAD_INIT = boom
    stop = threading.Event()
    t, out, started = _record_in_thread(m, 120.0, stop)
    deadline = time.monotonic() + 5
    while not fake.streams and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.recs == [120.0]
    stop.set()
    t.join(5)
    assert not t.is_alive(), "the fallback recording outlived the cancel"
    assert time.monotonic() - started < 5
    assert fake.streams[0].stops >= 1 and "result" in out


def test_stop_event_already_set_records_nothing(patched):
    fake = FakeSD([RuntimeError("no start")])
    m = _load(patched, fake)
    stop = threading.Event()
    stop.set()
    audio, _ = m.record_audio_with_silence_detection(120.0, stop_event=stop)
    assert fake.recs == [] and len(audio) == 0


def test_the_watcher_never_stops_an_earlier_stream(patched):
    """A stream from before the fallback (a finished earlier sd.rec) is never touched."""
    fake = FakeSD([RuntimeError("no start")])
    m = _load(patched, fake)
    old = FakeStream(0.0)
    old.finished.wait(1)
    old.close()
    fake.streams.append(old)
    stop = threading.Event()
    t, out, _ = _record_in_thread(m, 120.0, stop)
    deadline = time.monotonic() + 5
    while len(fake.streams) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(5)
    assert not t.is_alive() and old.stops == 0 and fake.streams[1].stops >= 1


def test_fallback_cap(patched, monkeypatch):
    monkeypatch.setenv("VOICEMODE_FALLBACK_RECORD_MAX_S", "0.2")
    fake = FakeSD([RuntimeError("no start")])
    m = _load(patched, fake)
    audio, _ = m.record_audio_with_silence_detection(120.0)
    assert fake.recs == [0.2] and len(audio) == 200


@pytest.mark.parametrize("value", ["", "abc", "0", "-1", "nan", "5"])
def test_fallback_cap_unset_invalid_or_larger_keeps_max_duration(patched, monkeypatch, value):
    monkeypatch.setenv("VOICEMODE_FALLBACK_RECORD_MAX_S", value)
    fake = FakeSD([RuntimeError("no start")])
    m = _load(patched, fake)
    m.record_audio_with_silence_detection(0.2)
    assert fake.recs == [0.2]


def test_default_fallback_without_stop_event_is_upstreams(tmp_path, shape, patched):
    _same_as_upstream(tmp_path, shape, patched, [RuntimeError("no start")])


def test_only_the_recording_paths_change(patched):
    fake = FakeSD([])
    m = _load(patched, fake)
    m.unrelated()
    assert fake.recs == [0.01]


# -- the patcher --------------------------------------------------------------------------

def test_idempotent(patched):
    once = patched.read_bytes()
    r = _run(patched)
    assert r.returncode == 0 and f"already patched {TAG}" in r.stdout
    assert patched.read_bytes() == once


def test_keeps_crlf(tmp_path):
    target = tmp_path / "converse.py"
    target.write_bytes(CONVERSE.replace("\n", "\r\n").encode("utf-8"))
    assert _run(target).returncode == 0
    data = target.read_bytes()
    assert MARKER.encode() in data and b"\n" not in data.replace(b"\r\n", b"")


@pytest.mark.parametrize("change", [
    ("def record_audio(duration: float) -> np.ndarray:", "def record(duration: float) -> np.ndarray:"),
    ("def record_audio_with_silence_detection(", "def record_vad("),
    ("                                                 'portaudio error']):\n", "                                                 'portaudio err']):\n"),
    ("                    return record_audio_with_silence_detection(max_duration, disable_silence_detection, min_duration, vad_aggressiveness, stop_event)\n", "                    return None\n"),
    ("            return (record_audio(max_duration), True)\n", "            return (record_audio(1.0), True)\n"),
])
def test_drift_fails_loudly_and_writes_nothing(tmp_path, change):
    assert CONVERSE.count(change[0]) == 1
    target = _write(tmp_path, CONVERSE.replace(*change))
    before = target.read_bytes()
    r = _run(target)
    assert r.returncode == 1 and "ANCHOR DRIFT" in r.stderr and TAG in r.stderr
    assert target.read_bytes() == before


def test_stdlib_only_and_no_provider_key_text():
    src = PATCHER.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in src and "CARTESIA_API_KEY" not in src
    for line in src.splitlines():
        if line.startswith(("import ", "from ")):
            assert line.split()[1].split(".")[0] in ("re", "sys", "pathlib"), line


# -- the real thing: pristine 8.12.0 and the whole manifest ------------------------------

def _src():
    raw = os.environ.get("VM812_SRC")
    if not raw:
        pytest.skip("VM812_SRC is not set")
    return Path(raw)


def _function(text):
    inner = "def record_audio_with_silence_detection_inner("
    start = text.index(inner if inner in text else "def record_audio_with_silence_detection(")
    return text[start:text.index("\ndef ", start + 1)]


def _check(text):
    body = _function(text)
    assert body.count("_vml_fallback_record(max_duration, stop_event)") == 2
    assert body.count("return (record_audio(max_duration), True)") == 2   # webrtcvad missing / disabled: intended
    assert "_vml_audio_recover_retry(record_audio_with_silence_detection," in body
    assert RETRY_LINE in body
    assert text.count("def _vml_fallback_record(") == 1
    compile(text, "converse.py", "exec")


def test_applies_to_pristine_8_12_0_and_is_idempotent(tmp_path):
    target = tmp_path / "converse.py"
    shutil.copy(_src() / "converse.py", target)
    r = _run(target)
    assert r.returncode == 0, r.stdout + r.stderr
    _check(target.read_text(encoding="utf-8"))
    once = target.read_bytes()
    r = _run(target)
    assert r.returncode == 0 and target.read_bytes() == once


def _manifest(name):
    return json.loads((PATCHES / name).read_text(encoding="utf-8"))["steps"]


@pytest.mark.parametrize("name", ["manifest.json", "manifest.windows.json"])
def test_both_manifests_carry_the_patch_after_the_recording_loop_patches(name):
    steps = _manifest(name)
    names = [s.get("patch") or s.get("copy") for s in steps]
    patch = next(s for s in steps if s.get("patch") == "patch_audio_recover.py")
    assert patch["targets"] == ["tools/converse.py"]
    for earlier in ("patch_listen_overrun.py", "patch_converse_hold.py", "patch_voice_barge.py",
                    "patch_voice_pill.py", "patch_ptt_instant.py", "patch_vad_override.py"):
        assert names.index(earlier) < names.index("patch_audio_recover.py"), earlier


@pytest.mark.parametrize("native", [False, True], ids=["default", "windows"])
def test_whole_manifest_applies(tmp_path, native):
    src = _src()
    pkg = tmp_path / "voice_mode"
    (pkg / "tools").mkdir(parents=True)
    sources = {"control_channel.py": "control_channel.py", "control_socket.py": "control_socket.py",
               "core.py": "core.py", "simple_failover.py": "simple_failover.py",
               "streaming.py": "streaming.py", "server.py": "server.py",
               "tools/converse.py": "converse.py", "conch_notify.py": "conch_notify.py",
               "conch_queue.py": "conch_queue.py", "conch.py": "conch.py", "config.py": "config.py",
               "tools/configuration_management.py": "configuration_management.py"}
    for rel, name in sources.items():
        shutil.copy(src / name, pkg / rel)
    args = [sys.executable, str(PATCHES / "apply.py"), "--package-dir", str(pkg)]
    if native:
        args.append("--native-windows")
    for _ in range(2):   # the second pass changes nothing
        r = subprocess.run(args, capture_output=True, text=True, env=dict(os.environ, PYTHONUTF8="1"))
        assert r.returncode == 0, r.stdout + r.stderr
    text = (pkg / "tools/converse.py").read_text(encoding="utf-8")
    _check(text)
    # The pill's Pause poll in record_audio is still there, and the retry goes through the
    # pill's wrapper (so the recording is still reported as listening).
    assert "while sd.get_stream().active:" in text
    assert "def record_audio_with_silence_detection_inner(" in text
    assert "_vml_silence_threshold_ms" in _function(text)   # the VAD override is intact


# Talos on FELIX #2104 (2): in the fixed-length fallback the pill shows Listening and offers
# Done, but nothing read skip_forward: the person waited up to max_duration.
class _Snap:
    def __init__(self, fwd):
        self.is_skip_forward = fwd


class _CtlState:
    def __init__(self):
        self.forward = False

    def snapshot(self):
        return _Snap(self.forward)


def _control(monkeypatch):
    state = _CtlState()
    cc = type(sys)("voice_mode.control_channel")
    cc.get_control_state = lambda: state
    monkeypatch.setitem(sys.modules, "voice_mode.control_channel", cc)
    return state


def test_done_ends_the_fallback_and_keeps_only_what_was_recorded(patched, monkeypatch):
    state = _control(monkeypatch)
    fake = FakeSD([RuntimeError("no start")])
    m = _load(patched, fake)
    t, out, started = _record_in_thread(m, 120.0, threading.Event())
    deadline = time.monotonic() + 5
    while not fake.streams and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.3)
    state.forward = True
    t.join(5)
    assert not t.is_alive(), "Done did not end the fallback recording"
    assert time.monotonic() - started < 5
    audio, _ = out["result"]
    assert 0 < len(audio) < 3 * m.SAMPLE_RATE      # not the 120 s buffer
    assert state.forward, "converse consumes skip_forward after the recording, not the watcher"
    assert any("skip_forward" in line for line in m.LOG)


def test_without_a_control_channel_the_fallback_is_as_before(patched, monkeypatch):
    monkeypatch.delitem(sys.modules, "voice_mode.control_channel", raising=False)
    fake = FakeSD([RuntimeError("no start")])
    m = _load(patched, fake)
    audio, _ = m.record_audio_with_silence_detection(0.3, stop_event=threading.Event())
    assert len(audio) == int(0.3 * m.SAMPLE_RATE)
