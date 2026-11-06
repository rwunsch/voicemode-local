"""Stop the assistant talking when you talk over it.

One process per machine. It opens the mic ONLY while voice-mode reports that TTS
is playing, runs Silero plus a volume gate, and sends ``barge_in`` once. The
server cuts the utterance and falls into its normal listen turn. ``barge_in`` is
a no-op unless TTS is live (patch_voice_barge.py), so a late detection cannot
end the turn that follows.

The words said before the server's own recording starts would otherwise be lost
(measured: only 1 of 4 counted words kept, with a 1.05 s gap). So the trigger keeps the
last 3 seconds of mic audio, keeps capturing until the server reports it is
listening, and hands that audio over in a private file for the server to prepend.

Why a volume gate: on speakers the mic hears the assistant, and Silero correctly
calls that speech. Measured 2026-11-04 on laptop speakers at full volume, the
echo peaked near -38 dBFS against about -18 for normal speech. Soft speech on
speakers can fall below the gate; see docs/voice-barge-in/README.md.

Streaming: an earlier note here said barge stays inert with
VOICEMODE_STREAMING_ENABLED=true. FELIX-MAC reported otherwise (cut playback with
streaming at its default); see docs/voice-barge-in/README.md.

Enable with ``voicemode-switch barge on``. The mic comes through parec on Linux and WSL,
through sounddevice (PortAudio) on Windows and macOS. On macOS with the built-in
speakers it comes through macOS voice processing (echo cancellation) instead, or not at
all without pyobjc-framework-AVFoundation; see open_capture.
Installed into voice_mode/voice_barge.py by patches/apply.sh.
"""
import ctypes
import logging
import os
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

try:
    from voice_mode.voice_pill_state import (  # noqa: F401  (re-exported for callers)
        _exchange, _retrying, _status, barge_control, control_socket_path, is_paused,
        barge_state, capture_backend, read_env_file, record_interruption, voicemode_base)
except ImportError:  # run straight from the repo, not the installed package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from voice_pill_state import (  # noqa: F401
        _exchange, _retrying, _status, barge_control, control_socket_path, is_paused,
        barge_state, capture_backend, read_env_file, record_interruption, voicemode_base)

logger = logging.getLogger("voicemode.voice_barge")

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512    # what Silero v5 takes at 16 kHz: 32 ms
WINDOW_S = WINDOW_SAMPLES / SAMPLE_RATE
CONTEXT_SAMPLES = 64

DEFAULT_GATE_DB = -40.0
DEFAULT_MIN_SPEECH_MS = 192
DEFAULT_THRESHOLD = 0.5
WARMUP_WINDOWS = 6      # ignore the mic's first ~190 ms: it wakes up noisily
POLL_EVERY_WINDOWS = 3  # re-check `speaking` about every 100 ms while listening
IDLE_POLL_S = 0.05
PREROLL_WINDOWS = 94       # about 3 s kept: soft speech took 2.5 s to detect
HANDOFF_MAX_WINDOWS = 94   # about 3 s: give up waiting for the server to listen
PREROLL_MAX_AGE_S = 15.0
PREROLL_HEADER = struct.Struct("<d")


class BargeDetector:
    """Fires once after enough consecutive windows of loud speech."""

    def __init__(self, gate_db: float, need_windows: int, threshold: float = DEFAULT_THRESHOLD):
        self.gate_db = gate_db
        self.need_windows = need_windows
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self._run = 0
        self._fired = False

    def feed(self, prob: float, db: float) -> bool:
        if self._fired:
            return False
        self._run = self._run + 1 if prob >= self.threshold and db >= self.gate_db else 0
        if self._run >= self.need_windows:
            self._fired = True
            return True
        return False


def window_db(window: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(window))) + 1e-9))


class SileroVad:
    def __init__(self, model_path: Path):
        import onnxruntime as ort  # imported late so the pure logic needs no model

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # must not compete with the audio pipeline
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def prob(self, window: np.ndarray) -> float:
        audio = np.concatenate([self._context, window])[None, :].astype(np.float32)
        out, self._state = self._session.run(
            None, {"input": audio, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
        )
        self._context = window[-CONTEXT_SAMPLES:]
        return float(out[0][0])


class ParecCapture:
    """The mic, via parec, only for as long as this object is open."""

    def __init__(self, device: str):
        self._proc = subprocess.Popen(
            ["parec", f"--device={device}", f"--rate={SAMPLE_RATE}", "--channels=1",
             "--format=s16le", "--latency-msec=30"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self._buf = b""

    def read_window(self):
        """One window, or None if the mic delivered nothing for 200 ms."""
        need = WINDOW_SAMPLES * 2
        fd = self._proc.stdout.fileno()
        while len(self._buf) < need:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                return None
            chunk = os.read(fd, need)
            if not chunk:
                return None
            self._buf += chunk
        raw, self._buf = self._buf[:need], self._buf[need:]
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def close(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._proc.stdout.close()


class UnknownDevice(Exception):
    """VOICEMODE_VOICE_BARGE_DEVICE names no input device."""


_LOGGED_DEVICES: set = set()


def resolve_input_device(sd, name: str) -> int:
    """The device index for a mic name.

    PortAudio lists one physical mic once per host API (MME, DirectSound, WASAPI,
    WDM-KS) and rejects a name that matches several. Take the match on the default
    input's host API, else the first match. Logs the choice once per device.
    """
    wanted = name.lower()
    matches = [i for i, d in enumerate(sd.query_devices())
               if d.get("max_input_channels", 0) > 0 and wanted in d["name"].lower()]
    if not matches:
        raise UnknownDevice(f"no input device matches {name!r}")
    chosen = matches[0]
    if len(matches) > 1:
        try:
            default_in = sd.default.device[0]
            default_api = sd.query_devices(default_in)["hostapi"] if default_in is not None and default_in >= 0 else None
        except Exception:
            default_api = None
        if default_api is not None:
            chosen = next((i for i in matches if sd.query_devices(i)["hostapi"] == default_api), chosen)
    if chosen not in _LOGGED_DEVICES:
        _LOGGED_DEVICES.add(chosen)
        info = sd.query_devices(chosen)
        try:
            api = sd.query_hostapis(info["hostapi"])["name"]
        except Exception:
            api = "?"
        logger.info("mic device: index %d, %s, host API %s (%d matched %r)",
                    chosen, info["name"], api, len(matches), name)
    return chosen


class SoundDeviceCapture:
    """The mic, via sounddevice (PortAudio), only for as long as this object is open.
    Same contract as ParecCapture: 512-sample float windows, None after 200 ms of nothing."""

    def __init__(self, device: str):
        import queue
        import sounddevice as sd
        self._queue = queue.Queue()
        self._buf = np.zeros(0, dtype=np.int16)
        setting = (device or "").strip()
        if setting in ("", "@DEFAULT_SOURCE@"):
            chosen = None
        elif setting.isdigit():
            chosen = int(setting)
        else:
            chosen = resolve_input_device(sd, setting)
        self._stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                      blocksize=WINDOW_SAMPLES, device=chosen,
                                      callback=self._on_block)
        self._stream.start()

    def _on_block(self, block, frames, time_info, status):
        self._queue.put(np.array(block[:, 0], dtype=np.int16))

    def read_window(self):
        import queue
        while len(self._buf) < WINDOW_SAMPLES:
            try:
                self._buf = np.concatenate([self._buf, self._queue.get(timeout=0.2)])
            except queue.Empty:
                return None
        raw, self._buf = self._buf[:WINDOW_SAMPLES], self._buf[WINDOW_SAMPLES:]
        return raw.astype(np.float32) / 32768.0

    def close(self) -> None:
        try:
            self._stream.stop()
        finally:
            self._stream.close()


# --- macOS: echo cancellation on the built-in speakers ---------------------------
#
# Plain capture on a MacBook's built-in speakers hears the assistant at the level of
# speech: barge cut itself off 2 of 2 (FELIX-MAC, 2026-11-06). macOS voice processing
# (AVAudioEngine, in this separate process) removed that echo in a spike. Headphones
# need none of it, so the route is read at each stretch start and decides the capture.

def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32), ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


_SYSTEM_OBJECT = 1                              # kAudioObjectSystemObject
_DEFAULT_OUTPUT = _fourcc("dOut")               # kAudioHardwarePropertyDefaultOutputDevice
_TRANSPORT = _fourcc("tran")                    # kAudioDevicePropertyTransportType
_DATA_SOURCE = _fourcc("ssrc")                  # kAudioDevicePropertyDataSource
_SCOPE_GLOBAL = _fourcc("glob")
_SCOPE_OUTPUT = _fourcc("outp")
_ELEMENT_MAIN = 0
_BUILT_IN = _fourcc("bltn")                     # kAudioDeviceTransportTypeBuiltIn
_HEADPHONE_JACK = _fourcc("hdpn")
_COREAUDIO_PATH = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
_COREAUDIO: dict = {}


def _platform() -> str:
    return sys.platform


def _coreaudio():
    """The CoreAudio library through ctypes, loaded once, or None."""
    if "lib" not in _COREAUDIO:
        try:
            lib = ctypes.CDLL(_COREAUDIO_PATH)
            fn = lib.AudioObjectGetPropertyData
            fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                           ctypes.POINTER(ctypes.c_uint32)]
            fn.restype = ctypes.c_int32
        except (OSError, AttributeError):
            lib = None
        _COREAUDIO["lib"] = lib
    return _COREAUDIO["lib"]


def _property_u32(lib, obj: int, selector: int, scope: int):
    """One UInt32 property, or None when CoreAudio says no (OSStatus not 0)."""
    address = _PropertyAddress(selector, scope, _ELEMENT_MAIN)
    size = ctypes.c_uint32(4)
    out = ctypes.c_uint32(0)
    status = lib.AudioObjectGetPropertyData(obj, ctypes.pointer(address), 0, None,
                                            ctypes.pointer(size), ctypes.pointer(out))
    return None if status != 0 else int(out.value)


def output_route(platform=None, lib=None) -> str:
    """Where the default output plays: "headphones", "speakers" or "unknown".

    Built-in device: 'hdpn' (headphone jack) is headphones; 'ispk', any other or an
    unreadable data source is speakers. Any other transport (Bluetooth, USB, HDMI,
    AirPlay, ...) counts as headphones. Off macOS, or when CoreAudio does not answer:
    unknown. Three property reads, no subprocess."""
    if (platform or _platform()) != "darwin":
        return "unknown"
    lib = lib if lib is not None else _coreaudio()
    if lib is None:
        return "unknown"
    try:
        device = _property_u32(lib, _SYSTEM_OBJECT, _DEFAULT_OUTPUT, _SCOPE_GLOBAL)
        if not device:
            return "unknown"
        transport = _property_u32(lib, device, _TRANSPORT, _SCOPE_GLOBAL)
        if transport is None:
            return "unknown"
        if transport != _BUILT_IN:
            return "headphones"
        source = _property_u32(lib, device, _DATA_SOURCE, _SCOPE_OUTPUT)
    except Exception:  # noqa: BLE001 - a route we cannot read is unknown, never a crash
        return "unknown"
    return "headphones" if source == _HEADPHONE_JACK else "speakers"


_VP_AVAILABLE: dict = {}


def voice_processing_available() -> bool:
    """Does AVFoundation (pyobjc-framework-AVFoundation) import here? Asked once."""
    if "ok" not in _VP_AVAILABLE:
        try:
            import AVFoundation  # noqa: F401
            _VP_AVAILABLE["ok"] = True
        except Exception:  # noqa: BLE001 - ImportError, or a broken pyobjc install
            _VP_AVAILABLE["ok"] = False
    return _VP_AVAILABLE["ok"]


ECHO_SETTING = "VOICEMODE_VOICE_BARGE_ECHO"


def echo_mode(raw) -> str:
    """VOICEMODE_VOICE_BARGE_ECHO: auto (default), on or off. Anything else is auto."""
    value = (raw or "").strip().strip('"').strip("'").lower()
    return value if value in ("auto", "on", "off") else "auto"


def choose_capture(route: str, echo: str, vp_available: bool) -> str:
    """"sounddevice", "voice-processing" or "refused", for one stretch on macOS.

    off: never voice processing. on: always, refused without AVFoundation. auto: only
    on the speakers, and there refused without AVFoundation, because plain capture on
    the speakers cuts the assistant off with its own voice."""
    if echo == "off":
        return "sounddevice"
    if echo == "on" or route == "speakers":
        return "voice-processing" if vp_available else "refused"
    return "sounddevice"


# Channel 0 of the voice-processed input tap. The spike on a MacBook Pro tapped 48 kHz,
# 9 channels, read channel 0 and the echo was gone. UNVERIFIED: that channel 0 is the
# processed (echo-cancelled) signal rather than one raw mic of the array; nothing
# documents the layout. tests/manual/mac_barge_vp_check.md checks it on a real Mac.
VP_CHANNEL = 0
VP_DUCKING_MINIMUM = (False, 10)   # (enableAdvancedDucking, AVAudioVoiceProcessingOtherAudioDuckingLevelMin)
VP_TAP_FRAMES = 4096


class _Resampler:
    """Streaming low-pass and resample to 16 kHz: no seams between buffers.

    A windowed-sinc FIR cuts everything above about 7.2 kHz first (plain [::3]
    folds 8-24 kHz back into the speech band), then each output sample is read by
    linear interpolation between filtered samples, which handles 44.1 kHz as well
    as 48 kHz."""

    TAPS = 127

    def __init__(self, in_rate: float, out_rate: int = SAMPLE_RATE):
        self.step = float(in_rate) / out_rate
        cutoff = 0.45 * out_rate / float(in_rate)          # cycles per input sample
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(self.TAPS)
        self._h = (h / h.sum()).astype(np.float64)
        self._history = np.zeros(self.TAPS - 1)
        self._last = 0.0
        self._pos = 0.0     # where the next output sample sits, in this chunk's filtered samples

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        filtered = np.convolve(np.concatenate([self._history, x]), self._h, mode="valid")
        self._history = np.concatenate([self._history, x])[-(self.TAPS - 1):]
        count = len(filtered)
        positions = self._pos + self.step * np.arange(int(np.floor((count - 1 - self._pos) / self.step)) + 1)
        # index 0 of `padded` is the previous chunk's last sample, at position -1
        padded = np.concatenate([[self._last], filtered])
        out = np.interp(positions + 1, np.arange(count + 1), padded)
        self._pos = (positions[-1] + self.step if len(positions) else self._pos) - count
        self._last = filtered[-1]
        return out.astype(np.float32)


def _ok(result) -> bool:
    """pyobjc returns (BOOL, NSError) for an error: out-parameter, or BOOL alone."""
    return bool(result[0]) if isinstance(result, tuple) else bool(result)


def _call_with_error(method, *args):
    """The spike called setVoiceProcessingEnabled_error_(True) with the error left out;
    some pyobjc versions want it passed as None. Try the spike's form first."""
    try:
        return method(*args)
    except TypeError:
        return method(*args, None)


def _channel_samples(buffer, channel: int) -> np.ndarray:
    """One channel of an AVAudioPCMBuffer of float32 as a numpy array (a copy)."""
    frames = int(buffer.frameLength())
    data = buffer.floatChannelData()
    stride = int(buffer.stride()) if hasattr(buffer, "stride") else 1
    if stride > 1:                         # interleaved: every channel behind pointer 0
        return _floats(data[0], frames * stride)[channel::stride].copy()
    return _floats(data[channel], frames).copy()


def _floats(pointer, count: int) -> np.ndarray:
    if hasattr(pointer, "as_buffer"):
        return np.frombuffer(pointer.as_buffer(count), dtype=np.float32)
    return np.fromiter((pointer[i] for i in range(count)), dtype=np.float32, count=count)


class _QueuedWindows:
    """read_window() over int16 blocks put on a queue by an audio callback."""

    def _init_queue(self):
        import queue
        self._queue = queue.Queue()
        self._buf = np.zeros(0, dtype=np.int16)

    def read_window(self):
        """One 512-sample float window at 16 kHz, or None after 200 ms of nothing."""
        import queue
        while len(self._buf) < WINDOW_SAMPLES:
            try:
                self._buf = np.concatenate([self._buf, self._queue.get(timeout=0.2)])
            except queue.Empty:
                return None
        raw, self._buf = self._buf[:WINDOW_SAMPLES], self._buf[WINDOW_SAMPLES:]
        return raw.astype(np.float32) / 32768.0


_DUCKING_LOGGED: set = set()


class VoiceProcessingCapture(_QueuedWindows):
    """macOS: the default mic through AVAudioEngine with voice processing (echo
    cancellation) on and other-audio ducking at its minimum. Same contract as
    SoundDeviceCapture. Ignores VOICEMODE_VOICE_BARGE_DEVICE: voice processing takes
    the system's default input.

    ``probe`` (VOICEMODE_VOICE_BARGE_VP_CHANNEL_PROBE=1) logs every tap channel's level
    at close, to settle which channel is the processed one (VP_CHANNEL)."""

    def __init__(self, probe: bool = False):
        import AVFoundation
        self._init_queue()
        self._tapped = False
        self._tap_failures = 0
        self._probe = probe
        self._probe_sumsq = None
        self._probe_frames = 0
        self._channels = 1
        self._engine = AVFoundation.AVAudioEngine.alloc().init()
        self._input = self._engine.inputNode()
        try:
            if not _ok(_call_with_error(self._input.setVoiceProcessingEnabled_error_, True)):
                raise RuntimeError("macOS refused to turn voice processing on")
            try:
                self._input.setVoiceProcessingOtherAudioDuckingConfiguration_(VP_DUCKING_MINIMUM)
            except (AttributeError, TypeError) as e:
                # AttributeError: before macOS 14. TypeError: pyobjc would not convert the
                # struct. Either way macOS's default ducking applies; voice processing stays on.
                if not _DUCKING_LOGGED:
                    _DUCKING_LOGGED.add(True)
                    logger.info("voice processing: other-audio ducking not settable (%s: %s); "
                                "default ducking applies", type(e).__name__, e)
            fmt = self._input.outputFormatForBus_(0)
            self._channels = int(fmt.channelCount())
            self._resampler = _Resampler(float(fmt.sampleRate()))
            self._input.installTapOnBus_bufferSize_format_block_(0, VP_TAP_FRAMES, fmt, self._on_buffer)
            self._tapped = True
            self._engine.prepare()
            if not _ok(_call_with_error(self._engine.startAndReturnError_)):
                raise RuntimeError("the voice-processing audio engine did not start")
        except Exception:
            self.close()
            raise

    def _on_buffer(self, buffer, when):
        try:
            mono = _channel_samples(buffer, VP_CHANNEL if self._channels > VP_CHANNEL else 0)
            if self._probe:
                self._probe_add(buffer)
            out = self._resampler.process(mono)
            self._queue.put((np.clip(out, -1.0, 1.0) * 32767).astype(np.int16))
        except Exception:  # noqa: BLE001 - never raise into CoreAudio's thread
            self._tap_failures += 1
            if self._tap_failures == 1:
                logger.exception("voice processing tap failed (further failures are counted, "
                                 "reported at close)")

    def _probe_add(self, buffer) -> None:
        sums = np.array([float(np.sum(np.square(_channel_samples(buffer, c).astype(np.float64))))
                         for c in range(self._channels)])
        self._probe_sumsq = sums if self._probe_sumsq is None else self._probe_sumsq + sums
        self._probe_frames += int(buffer.frameLength())

    def close(self) -> None:
        try:
            self._engine.stop()
        finally:
            try:  # best effort: hand the audio unit back without echo processing
                _call_with_error(self._input.setVoiceProcessingEnabled_error_, False)
            except Exception:  # noqa: BLE001
                pass
            if self._tapped:
                self._tapped = False
                self._input.removeTapOnBus_(0)
            if self._tap_failures:
                logger.warning("voice processing tap failed %d times in this capture", self._tap_failures)
            if self._probe and self._probe_frames:
                levels = 10 * np.log10(self._probe_sumsq / self._probe_frames + 1e-18)
                logger.info("vp channel probe: %d frames, rms per channel: %s", self._probe_frames,
                            ", ".join(f"{c} {db:.1f} dBFS" for c, db in enumerate(levels)))


class RefusedCapture:
    """No mic for this stretch (macOS speakers without echo cancellation). Carries the
    reason for the stretch line; run() waits the stretch out."""

    refused = True

    def __init__(self, note: str):
        self.stretch_note = note

    def read_window(self):
        return None

    def close(self) -> None:
        pass


class _RefusalLog:
    """Log a refusal once per route change, not at every poll."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._route = object()
        self._logged = False

    def seen(self, route: str) -> None:
        if route != self._route:
            self._route, self._logged = route, False

    def refuse(self, message: str) -> None:
        if not self._logged:
            self._logged = True
            logger.warning("%s", message)


_REFUSALS = _RefusalLog()
REFUSE_NO_AVFOUNDATION = ("barge: built-in speakers without echo cancellation; use headphones "
                          "(pyobjc-framework-AVFoundation missing)")
REFUSE_ECHO_ON = ("barge: VOICEMODE_VOICE_BARGE_ECHO=on but echo cancellation is unavailable "
                  "(pyobjc-framework-AVFoundation missing); not opening the mic")


def _plain_capture(backend: str, device: str):
    if backend == "parec":
        return ParecCapture(device)
    return SoundDeviceCapture(device)


VP_FAILED_NOTE = "refused (echo cancellation failed to start)"
NO_AVFOUNDATION_NOTE = "refused (pyobjc-framework-AVFoundation missing)"
_VP_FAILED: dict = {}   # route -> reason, for the current route only


def _vp_failure_path() -> Path:
    return voicemode_base() / "barge_vp_failed.json"


def _record_vp_failure(route: str, reason: str) -> None:
    """Remember for this process and tell `status` (another process) through a file."""
    import json
    _VP_FAILED.clear()
    _VP_FAILED[route] = reason
    try:
        path = _vp_failure_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{path}.tmp")
        tmp.write_text(json.dumps({"route": route, "reason": reason, "at": time.time()}))
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("could not record the voice-processing failure: %s", e)


def _clear_vp_failure() -> None:
    _VP_FAILED.clear()
    try:
        _vp_failure_path().unlink()
    except OSError:
        pass


def _vp_failed_on(route: str) -> bool:
    """Did the trigger record that voice processing failed to start on this route?"""
    import json
    try:
        return json.loads(_vp_failure_path().read_text()).get("route") == route
    except (OSError, ValueError, AttributeError):
        return False


def _label(choice: str, backend) -> str:
    """The capture name used in both the stretch line and status."""
    return (backend or "sounddevice") if choice == "sounddevice" else choice


def mac_capture_choice(echo: str):
    """(route, capture label) as the trigger's next stretch would choose them; for status."""
    route = output_route()
    choice = choose_capture(route, echo, voice_processing_available())
    if choice == "voice-processing" and _vp_failed_on(route):
        return route, VP_FAILED_NOTE
    if choice == "refused":
        return route, "refused"
    return route, _label(choice, capture_backend())


def open_capture(backend: str, device: str, platform=None, echo: str = "auto",
                 route=None, vp_available=None, vp_probe: bool = False):
    """The mic for one stretch of speech, by the backend capture_backend() chose.

    On macOS the output route decides (choose_capture): plain capture, voice
    processing, or a RefusedCapture: no mic this stretch. The capture carries
    ``stretch_note`` for the stretch line. Elsewhere: as before."""
    if (platform or _platform()) != "darwin":
        return _plain_capture(backend, device)
    where = (route or output_route)()
    if where not in _VP_FAILED:
        _VP_FAILED.clear()   # route changed: voice processing gets another try
    _REFUSALS.seen(where)
    choice = choose_capture(where, echo, (vp_available or voice_processing_available)())
    if choice == "refused":
        _REFUSALS.refuse(REFUSE_ECHO_ON if echo == "on" and where != "speakers" else REFUSE_NO_AVFOUNDATION)
        return RefusedCapture(f"route={where}, capture={NO_AVFOUNDATION_NOTE}")
    if choice == "voice-processing":
        if where in _VP_FAILED:
            return RefusedCapture(f"route={where}, capture={VP_FAILED_NOTE}")
        try:
            capture = VoiceProcessingCapture(probe=vp_probe)
        except Exception as e:  # noqa: BLE001 - never fall back to plain capture on the speakers
            _record_vp_failure(where, str(e))
            _REFUSALS.refuse(f"barge: echo cancellation did not start ({e}); not opening the mic")
            return RefusedCapture(f"route={where}, capture={VP_FAILED_NOTE}")
        if _VP_FAILED or _vp_failure_path().exists():
            _clear_vp_failure()
    else:
        capture = _plain_capture(backend, device)
    capture.stretch_note = f"route={where}, capture={_label(choice, backend)}"
    return capture


class StretchStats:
    """Levels of one speaking stretch, kept as a list of floats: no audio, no per-window log."""

    def __init__(self):
        self.dbs: list = []

    def add(self, db: float) -> None:
        self.dbs.append(db)

    def line(self, seconds: float, fired: bool, note=None) -> str:
        if self.dbs:
            peak = f"{max(self.dbs):.1f} dBFS"
            p90 = f"{float(np.percentile(self.dbs, 90)):.1f} dBFS"
        else:
            peak = p90 = "n/a"
        return (f"stretch: {seconds:.1f} s, windows {len(self.dbs)}, max {peak}, "
                f"p90 {p90}, fired {'yes' if fired else 'no'}" + (f", {note}" if note else ""))


def is_speaking(path: Path) -> bool:
    """True only if a server says TTS is playing."""
    return bool(_status(path).get("speaking"))


def preroll_path() -> Path:
    return voicemode_base() / "barge_preroll.bin"


def write_preroll(path: Path, samples: np.ndarray, start_t: float) -> None:
    """Hand audio to the server: atomic, and private because it holds voice."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    tmp = Path(f"{path}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(PREROLL_HEADER.pack(start_t) + pcm)
    _retrying(os.replace, tmp, path)  # Windows: the server may be reading the last one


def prepend_preroll(audio: np.ndarray, listen_started_at, sample_rate: int, path=None,
                    wait_s: float = 0.5, sleep=time.sleep) -> np.ndarray:
    """Called by the server: put the trigger's audio in front of its recording.

    Trimmed at the moment the server's stream opened, so nothing is doubled. Any
    file that does not fit this recording is discarded rather than guessed at.
    """
    path = Path(path) if path is not None else preroll_path()
    waited = 0.0
    while not path.exists() and waited < wait_s:
        sleep(0.02)
        waited += 0.02
    # Server side, after recording: one read and one delete, never a retry sleep.
    try:
        raw = path.read_bytes()
    except OSError:
        return audio  # unreadable: leave it; the age check discards it later
    try:
        os.unlink(path)
    except OSError:
        pass  # Windows, still busy: the age check discards a leftover
    if len(raw) < PREROLL_HEADER.size or listen_started_at is None:
        return audio
    start_t = PREROLL_HEADER.unpack_from(raw)[0]
    held_s = listen_started_at - start_t
    if not 0 < held_s <= PREROLL_MAX_AGE_S:
        return audio
    pcm = np.frombuffer(raw, dtype=np.int16, offset=PREROLL_HEADER.size).astype(np.float32)
    pcm = pcm[:int(held_s * SAMPLE_RATE)]
    if len(pcm) == 0:
        return audio
    count = int(len(pcm) * sample_rate / SAMPLE_RATE)
    resampled = np.interp(np.linspace(0, len(pcm) - 1, count), np.arange(len(pcm)), pcm)
    return np.concatenate([resampled.astype(np.int16), np.asarray(audio).reshape(-1).astype(np.int16)])


def send_barge_in(path: Path) -> None:
    try:
        _exchange(path, {"command": "barge_in"}, want_reply=False)
    except OSError as e:
        logger.debug("barge_in not delivered: %s", e)


def _hand_off(capture, held, poll_listen_started, preroll_sink, poll_every_windows, clock) -> None:
    """Keep capturing until the server is listening, then pass on all of it."""
    last_read = clock()
    for count in range(1, HANDOFF_MAX_WINDOWS + 1):
        window = capture.read_window()
        if window is not None:
            held.append(window)
            last_read = clock()
        if count % poll_every_windows == 0 and poll_listen_started() is not None:
            break
    samples = np.concatenate(list(held))
    preroll_sink(samples, last_read - len(samples) / SAMPLE_RATE)


def run(poll_speaking, open_capture, vad, detector, send_barge, sleep=time.sleep,
        warmup_windows=WARMUP_WINDOWS, poll_every_windows=POLL_EVERY_WINDOWS,
        idle_poll_s=IDLE_POLL_S, poll_listen_started=None, preroll_sink=None,
        clock=time.time, preroll_windows=PREROLL_WINDOWS,
        is_paused=lambda: False, on_fire=None, stretch_clock=time.monotonic) -> None:
    """Main loop. ``poll_speaking`` returns True, False, or None to shut down.

    While ``is_paused()`` the mic stays closed (and an open one is closed at the
    next poll). ``on_fire()`` is called once per barge-in sent.
    """
    while True:
        speaking = poll_speaking()
        if speaking is None:
            return
        if not speaking or is_paused():
            sleep(idle_poll_s)
            continue

        detector.reset()
        vad.reset()
        asked_at = stretch_clock()
        capture = open_capture()
        if capture is None or getattr(capture, "refused", False):
            # refused (macOS speakers without echo cancellation): wait this one out
            while True:
                speaking = poll_speaking()
                if speaking is None or not speaking:
                    break
                sleep(idle_poll_s)
            if capture is not None:
                logger.info("%s", StretchStats().line(stretch_clock() - asked_at, False, capture.stretch_note))
            if speaking is None:
                return
            continue
        fired = False
        count = 0
        recent = deque(maxlen=preroll_windows)
        stats = StretchStats()
        opened_at = stretch_clock()
        first_window_at = None
        try:
            while True:
                window = capture.read_window()
                count += 1
                if window is not None and first_window_at is None:
                    first_window_at = stretch_clock()
                if count % poll_every_windows == 0:
                    speaking = poll_speaking()
                    if speaking is None:
                        return
                    if not speaking or is_paused():
                        break
                if window is None or count <= warmup_windows:
                    continue
                recent.append(window)
                prob = vad.prob(window)
                db = window_db(window)
                stats.add(db)
                if detector.feed(prob, db):
                    send_barge()
                    if on_fire is not None:
                        try:
                            on_fire()
                        except Exception:
                            logger.exception("on_fire failed")
                    logger.info("barge-in sent: prob=%.2f level=%.1f dBFS", prob, db)
                    fired = True
                    if preroll_sink is not None:
                        _hand_off(capture, list(recent), poll_listen_started, preroll_sink,
                                  poll_every_windows, clock)
                    break
        finally:
            try:
                note = getattr(capture, "stretch_note", None)
                if note:  # macOS: how long until the mic (voice processing) gave audio
                    first = ("none" if first_window_at is None
                             else f"{round((first_window_at - asked_at) * 1000)} ms")
                    note = f"first_window {first}, {note}"
                logger.info("%s", stats.line(stretch_clock() - opened_at, fired, note))
            finally:
                capture.close()

        # Don't reopen the mic for the utterance that is being cut.
        while fired:
            speaking = poll_speaking()
            if speaking is None:
                return
            if not speaking:
                break
            sleep(idle_poll_s)


def trigger_settings() -> dict:
    """The VOICEMODE_VOICE_BARGE* settings: the environment over ~/.voicemode/voicemode.env."""
    def ours(d):
        return {k: v for k, v in d.items() if k.startswith("VOICEMODE_VOICE_BARGE")}
    return {**ours(read_env_file()), **ours(os.environ)}


def install_stop_handlers(stop: threading.Event) -> None:
    """Ctrl-C / SIGTERM (and Ctrl-Break on Windows) set ``stop``. Signals the platform
    lacks, or a call off the main thread, are skipped: the owner then stops us by
    ending the process."""
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass


CANNOT_RUN = ("cannot run: voice barge-in needs a mic reader, parec (PulseAudio or PipeWire) "
              "or the sounddevice package, and this machine has neither")


def status_line() -> str:
    """on, off or cannot run: <reason>. What FELIX reads at voice start. On macOS, when
    it can run, a second line: ``route: <route>, capture: <capture or refused>``."""
    state = barge_state()
    if state is None:
        return CANNOT_RUN
    first = "on" if state else "off"
    if _platform() != "darwin":
        return first
    route, capture = mac_capture_choice(echo_mode(trigger_settings().get(ECHO_SETTING)))
    return f"{first}\nroute: {route}, capture: {capture}"


def main() -> int:
    if sys.argv[1:2] == ["status"]:
        print(status_line())
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = trigger_settings()
    backend = capture_backend()
    if backend is None:
        logger.error(CANNOT_RUN)
        return 1
    if len(sys.argv) < 2:
        logger.error("usage: python -m voice_mode.voice_barge <silero_vad.onnx> | status")
        return 2
    model = Path(sys.argv[1])
    if not model.is_file():
        logger.error("Silero model not found at %s; run: voicemode-switch barge on", model)
        return 1

    gate_db = float(settings.get("VOICEMODE_VOICE_BARGE_GATE_DB", DEFAULT_GATE_DB))
    min_ms = float(settings.get("VOICEMODE_VOICE_BARGE_MIN_SPEECH_MS", DEFAULT_MIN_SPEECH_MS))
    device = settings.get("VOICEMODE_VOICE_BARGE_DEVICE", "@DEFAULT_SOURCE@")
    need = max(1, round(min_ms / (WINDOW_S * 1000)))
    if backend == "sounddevice" and device.strip() not in ("", "@DEFAULT_SOURCE@") and not device.strip().isdigit():
        try:
            import sounddevice as sd
            resolve_input_device(sd, device.strip())
        except UnknownDevice as e:
            logger.error("%s; set VOICEMODE_VOICE_BARGE_DEVICE to part of an input device's name, "
                         "or a device number", e)
            return 1

    echo = echo_mode(settings.get(ECHO_SETTING))
    vp_probe = settings.get("VOICEMODE_VOICE_BARGE_VP_CHANNEL_PROBE", "").strip().strip('"').lower() in ("1", "true")
    if _platform() == "darwin":
        _clear_vp_failure()  # a failure recorded by an earlier trigger no longer holds
        logger.info("macOS: %s=%s, echo cancellation (AVFoundation) %s, route now %s",
                    ECHO_SETTING, echo, "available" if voice_processing_available() else "missing",
                    output_route())

    stop = threading.Event()
    install_stop_handlers(stop)

    path = control_socket_path()
    logger.info("started: gate=%.1f dBFS need=%d windows device=%s mic=%s socket=%s",
                gate_db, need, device, backend, path)
    run(
        poll_speaking=lambda: None if stop.is_set() else is_speaking(path),
        open_capture=lambda: open_capture(backend, device, echo=echo, vp_probe=vp_probe),
        vad=SileroVad(model),
        detector=BargeDetector(gate_db, need),
        send_barge=lambda: send_barge_in(path),
        poll_listen_started=lambda: _status(path).get("listen_started_at"),
        preroll_sink=lambda samples, start_t: write_preroll(preroll_path(), samples, start_t),
        is_paused=is_paused,
        on_fire=lambda: record_interruption(time.time()),
    )
    logger.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
