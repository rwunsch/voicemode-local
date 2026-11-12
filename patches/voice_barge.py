"""Stop the assistant talking when you talk over it.

One process per machine. The mic is open only during a converse turn, at three
times, and closed otherwise (idle, between replies, while paused):

- "warm": the turn is generating its reply and has neither spoken nor listened
  yet (PhaseTracker; VOICEMODE_VOICE_BARGE_WARM=off turns it off). This only lets
  the mic start up before playback; every window read while warm is discarded,
  never fed to Silero nor kept.
- "speaking": voice-mode reports that TTS is playing. Silero plus a volume gate
  run on this audio, and ``barge_in`` is sent once.
- after a barge: until the server reports it is listening (at most about 3 s),
  to hand over the words said in between (below). A pause stops it at once.

The server cuts the utterance and falls into its normal listen turn. ``barge_in`` is
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
        barge_mode, barge_state, capture_backend, read_env_file, record_interruption, voicemode_base)
except ImportError:  # run straight from the repo, not the installed package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from voice_pill_state import (  # noqa: F401
        _exchange, _retrying, _status, barge_control, control_socket_path, is_paused,
        barge_mode, barge_state, capture_backend, read_env_file, record_interruption, voicemode_base)

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

    def drain(self) -> None:
        """Throw away everything captured so far, buffered or still in the pipe."""
        self._buf = b""
        fd = self._proc.stdout.fileno()
        while select.select([fd], [], [], 0)[0]:
            if not os.read(fd, 65536):
                break

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
        try:
            self._stream.start()
        except Exception:
            self._stream.close()   # never leave an opened PortAudio stream behind
            raise

    def _on_block(self, block, frames, time_info, status):
        self._queue.put(np.array(block[:, 0], dtype=np.int16))

    def drain(self) -> None:
        """Throw away everything captured so far, buffered or still queued."""
        import queue
        self._buf = np.zeros(0, dtype=np.int16)
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

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
_SCOPE_INPUT = _fourcc("inpt")
_STREAMS = _fourcc("stm#")                      # kAudioDevicePropertyStreams
_ELEMENT_MAIN = 0
_BUILT_IN = _fourcc("bltn")                     # kAudioDeviceTransportTypeBuiltIn
_HEADPHONE_JACK = _fourcc("hdpn")
_BLUETOOTH = (_fourcc("blue"), _fourcc("blea"))  # kAudioDeviceTransportTypeBluetooth, ...BluetoothLE
_DISPLAY_OR_AIRPLAY = (_fourcc("hdmi"), _fourcc("dprt"), _fourcc("airp"))
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
            size_fn = lib.AudioObjectGetPropertyDataSize
            size_fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            size_fn.restype = ctypes.c_int32
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


def _input_stream_count(lib, device: int):
    """How many input streams the device has, or None when CoreAudio says no."""
    address = _PropertyAddress(_STREAMS, _SCOPE_INPUT, _ELEMENT_MAIN)
    size = ctypes.c_uint32(0)
    status = lib.AudioObjectGetPropertyDataSize(device, ctypes.pointer(address), 0, None,
                                                ctypes.pointer(size))
    return None if status != 0 else int(size.value) // 4   # one AudioStreamID each


# -- Windows: the default playback endpoint's form factor (Core Audio) --------
# Measured 2026-11-10 on Robert's laptop (one read per device): Jabra Engage 75 = 5
# (Headset, USB); Realtek "Speakers" = 1 (INTELAUDIO); VB-Audio cables = 2 (LineLevel,
# ROOT). A read took 2.15 ms (C#). Many laptop drivers expose the headphone jack and the
# speakers as one "Speakers" endpoint: that reads speakers even with headphones plugged in,
# the safe direction (auto stays off, it can never cut itself off).
_WIN_HEADPHONES = {3, 5, 6}        # Headphones, Headset, Handset
_WIN_SPEAKERS = {1, 9}             # Speakers, DigitalAudioDisplayDevice (HDMI/DP)
_WIN_BLUETOOTH = ("BTHENUM", "BTHHFENUM", "BTHLEDEVICE", "BTHLE")


def windows_route(form_factor, enumerator) -> str:
    """Classify a Windows playback endpoint. Bluetooth is headphones, as on macOS."""
    if isinstance(enumerator, str) and enumerator.upper().startswith(_WIN_BLUETOOTH):
        return "headphones"
    if form_factor in _WIN_HEADPHONES:
        return "headphones"
    if form_factor in _WIN_SPEAKERS:
        return "speakers"
    return "unknown"


def _windows_default_endpoint():
    """(form factor, enumerator name) of the default console playback endpoint, or None.
    Plain ctypes COM: IMMDeviceEnumerator -> IMMDevice -> IPropertyStore, by vtable slot."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                    ("d4", ctypes.c_ubyte * 8)]

    def guid(text):
        g = GUID()
        if ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(g)):
            raise OSError("bad GUID")
        return g

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                    ("r3", ctypes.c_ushort), ("val", ctypes.c_uint64), ("pad", ctypes.c_uint64)]

    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)   # COINIT_MULTITHREADED; an existing apartment is fine

    def call(obj, slot, *argtypes_and_args):
        argtypes = [t for t, _ in argtypes_and_args]
        args = [a for _, a in argtypes_and_args]
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[slot])
        return fn(obj, *args)

    def release(obj):
        if obj:
            call(obj, 2)

    enum = ctypes.c_void_p()
    if ole32.CoCreateInstance(ctypes.byref(guid("BCDE0395-E52F-467C-8E3D-C4579291692E")), None, 1,
                              ctypes.byref(guid("A95664D2-9614-4F35-A746-DE8DB63617E6")),
                              ctypes.byref(enum)):
        return None
    device, store = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        # GetDefaultAudioEndpoint(eRender=0, eConsole=0): what PortAudio's default plays to.
        if call(enum, 4, (ctypes.c_int, 0), (ctypes.c_int, 0), (ctypes.c_void_p, ctypes.byref(device))):
            return None
        if call(device, 4, (wintypes.DWORD, 0), (ctypes.c_void_p, ctypes.byref(store))):  # OpenPropertyStore(STGM_READ)
            return None

        def value(fmtid, pid):
            key = PROPERTYKEY(guid(fmtid), pid)
            pv = PROPVARIANT()
            if call(store, 5, (ctypes.c_void_p, ctypes.byref(key)), (ctypes.c_void_p, ctypes.byref(pv))):
                return None
            try:
                if pv.vt == 19:        # VT_UI4
                    return int(pv.val & 0xFFFFFFFF)
                if pv.vt == 31 and pv.val:   # VT_LPWSTR
                    return ctypes.wstring_at(pv.val)
                return None
            finally:
                ole32.PropVariantClear(ctypes.byref(pv))

        form = value("1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E", 0)        # PKEY_AudioEndpoint_FormFactor
        enumerator = value("A45C254E-DF1C-4EFD-8020-67D146A850E0", 24)  # PKEY_Device_EnumeratorName
        return form, enumerator
    finally:
        release(store)
        release(device)
        release(enum)


def output_route(platform=None, lib=None) -> str:
    """Where the default output plays: "headphones", "speakers" or "unknown".

    By transport, in this order:
    - built-in ('bltn'): 'hdpn' (headphone jack) is headphones; 'ispk', any other or an
      unreadable data source is speakers.
    - Bluetooth ('blue', 'blea'): headphones, never asking for input streams. Measured on
      FELIX-MAC 2026-11-07: a Jabra Engage 75 is two CoreAudio devices, "1 in, 0 out" and
      "0 in, 2 out", so its output device has no input streams; the input-stream rule
      (a2ef7e3) called it speakers and put voice processing on a headset. A Bluetooth
      speaker now counts as headphones too: no echo cancellation for it.
    - HDMI, DisplayPort, AirPlay ('hdmi', 'dprt', 'airp'): speakers.
    - anything else (USB, virtual, ...): headphones if the output device also has input
      streams (a headset), speakers if none, unknown if unreadable.
    On Windows: the default playback endpoint's form factor (windows_route).
    Elsewhere, or when the OS does not answer: unknown. At most three property reads,
    no subprocess."""
    plat = platform or _platform()
    if plat == "win32":
        try:
            read = lib() if callable(lib) else _windows_default_endpoint()
        except Exception:  # noqa: BLE001 - a route we cannot read is unknown, never a crash
            return "unknown"
        return "unknown" if read is None else windows_route(*read)
    if plat != "darwin":
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
        if transport in _BLUETOOTH:
            return "headphones"
        if transport in _DISPLAY_OR_AIRPLAY:
            return "speakers"
        if transport != _BUILT_IN:
            inputs = _input_stream_count(lib, device)
            if inputs is None:
                return "unknown"
            return "headphones" if inputs > 0 else "speakers"
        source = _property_u32(lib, device, _DATA_SOURCE, _SCOPE_OUTPUT)
    except Exception:  # noqa: BLE001 - a route we cannot read is unknown, never a crash
        return "unknown"
    return "headphones" if source == _HEADPHONE_JACK else "speakers"


def output_is_bluetooth(platform=None, lib=None) -> bool:
    """Is the default output a Bluetooth device (transport 'blue' or 'blea')? Two property
    reads; False off macOS, when CoreAudio does not answer, or on any error."""
    if (platform or _platform()) != "darwin":
        return False
    lib = lib if lib is not None else _coreaudio()
    if lib is None:
        return False
    try:
        device = _property_u32(lib, _SYSTEM_OBJECT, _DEFAULT_OUTPUT, _SCOPE_GLOBAL)
        if not device:
            return False
        return _property_u32(lib, device, _TRANSPORT, _SCOPE_GLOBAL) in _BLUETOOTH
    except Exception:  # noqa: BLE001 - unreadable is "not Bluetooth"
        return False


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


def choose_capture(route: str, echo: str, vp_available: bool, bluetooth: bool = False) -> str:
    """"sounddevice", "voice-processing" or "refused", for one stretch on macOS.

    off: never voice processing. on: always, refused without AVFoundation. auto: only
    on the speakers, and there refused without AVFoundation, because plain capture on
    the speakers cuts the assistant off with its own voice. Bluetooth is always plain
    capture, even for ``on``: measured on a Mac 2026-11-07 (n=2), voice processing on a
    Bluetooth headset stops itself within 0.17 s (configuration change, engine not running,
    no taps)."""
    if echo == "off" or bluetooth:
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

    def drain(self) -> None:
        """Throw away everything captured so far, buffered or still queued."""
        import queue
        self._buf = np.zeros(0, dtype=np.int16)
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

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


def _yes(flag) -> str:
    return "unknown" if flag is None else ("yes" if flag else "no")


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
        self._opened_at = time.monotonic()
        self._taps = 0
        self._samples = 0
        self._first_tap_at = None
        self._running_after_start = None
        self._config_changes = 0
        self._warned_stopped = False
        self._observer = None
        self._center = None
        self._engine = AVFoundation.AVAudioEngine.alloc().init()
        self._input = self._engine.inputNode()
        self._observe_config_changes(AVFoundation)
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
            self._running_after_start = self._engine_running()
            logger.debug("voice processing %#x: engine started, running %s, %d ch at %.0f Hz",
                         id(self), _yes(self._running_after_start), self._channels,
                         float(fmt.sampleRate()))
        except Exception:
            self.close()
            raise

    def _observe_config_changes(self, av) -> None:
        """Log AVAudioEngineConfigurationChangeNotification for this engine: macOS posts
        it when the device under the engine changes (and stops the engine). Best effort."""
        try:
            center = av.NSNotificationCenter.defaultCenter()
            self._observer = center.addObserverForName_object_queue_usingBlock_(
                av.AVAudioEngineConfigurationChangeNotification, self._engine, None,
                self._on_config_change)
            self._center = center
        except Exception as e:  # noqa: BLE001 - diagnostics only
            logger.debug("voice processing: cannot observe configuration changes (%s)", e)

    def _on_config_change(self, note) -> None:
        self._config_changes += 1
        running = self._engine_running()
        logger.info("voice processing %#x: engine configuration changed (device change?), "
                    "engine running %s, taps so far %d", id(self), _yes(running), self._taps)
        if running is False and not self._warned_stopped:
            self._warned_stopped = True   # once per stretch: one capture is one stretch
            logger.warning("barge: echo cancellation stopped by a device change")

    def _engine_running(self):
        try:
            return bool(self._engine.isRunning())
        except Exception:  # noqa: BLE001
            return None

    def diagnostics(self) -> str:
        """For the stretch line: did the tap fire, and is the engine still running?"""
        first = ("none" if self._first_tap_at is None
                 else f"{round((self._first_tap_at - self._opened_at) * 1000)} ms")
        return (f"taps {self._taps}, samples {self._samples}, engine_running "
                f"{_yes(self._engine_running())} (after start {_yes(self._running_after_start)}), "
                f"first_tap {first}, config_changes {self._config_changes}")

    def _on_buffer(self, buffer, when):
        self._taps += 1
        if self._first_tap_at is None:
            self._first_tap_at = time.monotonic()
        try:
            self._samples += int(buffer.frameLength())
        except Exception:  # noqa: BLE001 - counted as a tap failure below
            pass
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
        if self._observer is not None:
            try:
                self._center.removeObserver_(self._observer)
            except Exception:  # noqa: BLE001
                pass
            self._observer = None
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
    choice = choose_capture(route, echo, voice_processing_available(),
                            bluetooth=echo == "on" and output_is_bluetooth())
    if choice == "voice-processing" and _vp_failed_on(route):
        return route, VP_FAILED_NOTE
    if choice == "refused":
        return route, "refused"
    return route, _label(choice, capture_backend())


def open_capture(backend: str, device: str, platform=None, echo: str = "auto",
                 route=None, vp_available=None, vp_probe: bool = False, bluetooth=None):
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
    # Only `on` needs the question: `auto` is plain on every Bluetooth route already.
    is_bt = echo == "on" and bool((bluetooth or output_is_bluetooth)())
    choice = choose_capture(where, echo, (vp_available or voice_processing_available)(), bluetooth=is_bt)
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
    if is_bt:
        capture.stretch_note += (" (Bluetooth: VOICEMODE_VOICE_BARGE_ECHO=on not applied, "
                                 "voice processing stops itself on a Bluetooth headset)")
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


def _hand_off(capture, held, poll_listen_started, preroll_sink, poll_every_windows, clock,
              is_paused=lambda: False, preroll_discard=None) -> None:
    """Keep capturing until the server is listening, then pass on all of it.

    Pause stops it at once: no further window is read, nothing is handed over, and any
    earlier preroll file is removed (``preroll_discard``). A sink that fails (Windows:
    os.replace refused while the server reads the last file) costs this preroll only."""
    last_read = clock()
    for count in range(1, HANDOFF_MAX_WINDOWS + 1):
        if is_paused():
            if preroll_discard is not None:
                try:
                    preroll_discard()
                except OSError as e:
                    logger.debug("stale preroll not removed: %s", e)
            return
        window = capture.read_window()
        if window is not None:
            held.append(window)
            last_read = clock()
        if count % poll_every_windows == 0 and poll_listen_started() is not None:
            break
    samples = np.concatenate(list(held))
    try:
        preroll_sink(samples, last_read - len(samples) / SAMPLE_RATE)
    except OSError as e:
        logger.warning("preroll not handed over (%s); the words before listening are lost "
                       "this time", e)


MIC_PHASES = ("warm", "speaking")
WARM_SETTING = "VOICEMODE_VOICE_BARGE_WARM"
WARM_POLL_EVERY_WINDOWS = 1  # while warm, see `speaking` within one window (32 ms)


def warm_enabled(raw) -> bool:
    """VOICEMODE_VOICE_BARGE_WARM: on unless 0, false, off or no."""
    value = (raw or "").strip().strip('"').strip("'").lower()
    return value not in ("0", "false", "off", "no")


class PhaseTracker:
    """What the mic should do now, from the server's status: "idle", "warm" or "speaking".

    Measured before this existed: the mic opened only once `speaking` showed, and it then
    took 160 ms (Windows, MME) to 800-900 ms (macOS, reported) to give audio, plus the
    192 ms warmup, so the first second of every reply could not be interrupted.

    "warm": a converse audio operation is running (its control socket answers only then,
    and only after the conch is taken), the server has neither spoken nor listened yet in
    it, and it reports ``listening`` (patch_voice_pill), so there is a signal to let go.
    That is the TTS generation time, which covers the mic's start-up. After the first
    stretch of speech, or once the server listens, the mic stays closed until the
    operation ends: nothing is kept open between replies or while idle."""

    def __init__(self, status, warm: bool = True):
        self._status = status
        self._warm = warm
        self._spent = False
        self._last = ("idle", "no converse running")

    def __call__(self) -> str:
        phase, why = self._decide()
        if (phase, why) != self._last:   # one DEBUG line per change, not per poll
            self._last = (phase, why)
            logger.debug("phase %s%s", phase, f": {why}" if why else "")
        return phase

    def _decide(self):
        s = self._status()
        if not s:
            self._spent = False   # no converse running: the next one may warm again
            return "idle", "no converse running"
        if s.get("speaking"):
            self._spent = True
            return "speaking", ""
        if s.get("listening"):
            self._spent = True
            return "idle", "listening"
        if not self._warm:
            return "idle", "warm off"
        if self._spent:
            return "idle", "spoken already in this converse"
        if "listening" not in s:
            return "idle", "status has no 'listening' (server without patch_voice_pill): no warm"
        if s.get("state") in ("stopped", "paused"):
            return "idle", f"state {s.get('state')}"
        return "warm", "converse running, not yet spoken"


class _Stretch:
    """One stretch of speech within an open capture: timing and levels for its log line."""

    def __init__(self, asked_at: float, warm_s):
        self.asked_at = asked_at
        self.warm_s = warm_s          # how long the mic had been open already; None: cold
        self.first_window_at = None
        self.heard_at = None          # the first window that reached the detector
        self.stats = StretchStats()

    def timing(self) -> str:
        def ms(t):
            return "none" if t is None else f"{round((t - self.asked_at) * 1000)} ms"
        warm = "cold" if self.warm_s is None else f"warm {round(self.warm_s * 1000)} ms"
        return f"first_window {ms(self.first_window_at)}, deaf {ms(self.heard_at)}, {warm}"


def _note_with_diagnostics(capture):
    """The capture's stretch note plus its diagnostics (macOS voice processing: taps,
    engine state), read before close."""
    note = getattr(capture, "stretch_note", None)
    diag = getattr(capture, "diagnostics", None)
    if diag is None:
        return note
    try:
        extra = diag()
    except Exception as e:  # noqa: BLE001 - diagnostics never cost a stretch line
        extra = f"diagnostics failed: {e}"
    return f"{note}, {extra}" if note else extra


def _log_stretch(stretch: _Stretch, seconds: float, fired: bool, note) -> None:
    timing = stretch.timing()
    logger.info("%s", stretch.stats.line(seconds, fired, f"{timing}, {note}" if note else timing))


def _wait_out_refused(capture, phase, poll_phase, is_paused, sleep, idle_poll_s, stretch_clock):
    """No mic this time (macOS speakers without echo cancellation): wait until the mic
    would close, with one stretch line per stretch of speech. None: shut down."""
    note = getattr(capture, "stretch_note", None) if capture is not None else None
    began = None
    while True:
        if phase == "speaking" and began is None:
            began = stretch_clock()
        if phase != "speaking" and began is not None:
            if note:
                logger.info("%s", StretchStats().line(stretch_clock() - began, False, note))
            began = None
        if phase is None:
            return None
        if phase not in MIC_PHASES:
            return True
        sleep(idle_poll_s)
        phase = poll_phase()
        if phase is not None and is_paused():
            phase = "idle"


def run(poll_speaking=None, open_capture=None, vad=None, detector=None, send_barge=None,
        sleep=time.sleep, warmup_windows=WARMUP_WINDOWS, poll_every_windows=POLL_EVERY_WINDOWS,
        idle_poll_s=IDLE_POLL_S, poll_listen_started=None, preroll_sink=None,
        clock=time.time, preroll_windows=PREROLL_WINDOWS,
        is_paused=lambda: False, on_fire=None, stretch_clock=time.monotonic,
        poll_phase=None, warm_poll_every_windows=WARM_POLL_EVERY_WINDOWS,
        preroll_discard=None) -> None:
    """Main loop. ``poll_phase`` returns "idle", "warm", "speaking", or None to shut
    down (see PhaseTracker); ``poll_speaking`` (True, False or None) is the older form,
    with no warm phase.

    The mic is open while warm or speaking. Windows read while warm are thrown away:
    they only let the mic start up and spend the warmup before the reply plays. While
    ``is_paused()`` the mic stays closed (an open one closes at the next poll).
    ``on_fire()`` is called once per barge-in sent.

    After a barge the mic keeps reading until the server listens, then hands the audio to
    ``preroll_sink(samples, start_t)``; a pause in that time drops it and calls
    ``preroll_discard()``. A mic that fails to open is retried at the next stretch, with
    one warning per run of consecutive failures.
    """
    if poll_phase is None:
        def poll_phase():
            speaking = poll_speaking()
            return None if speaking is None else ("speaking" if speaking else "idle")

    open_failing = False   # one warning per run of consecutive failed opens
    while True:
        phase = poll_phase()
        if phase is None:
            return
        if phase not in MIC_PHASES or is_paused():
            sleep(idle_poll_s)
            continue

        opened_at = stretch_clock()
        try:
            capture = open_capture()
        except Exception as e:  # noqa: BLE001 - a mic that will not open costs this stretch only
            if not open_failing:
                logger.warning("mic did not open (%s: %s); retrying at the next stretch",
                               type(e).__name__, e)
            open_failing = True
            capture = None
        else:
            open_failing = False
        if capture is not None:
            logger.debug("capture open: %s id=%#x, phase %s", type(capture).__name__,
                         id(capture), phase)
        if capture is None or getattr(capture, "refused", False):
            if _wait_out_refused(capture, phase, poll_phase, is_paused, sleep, idle_poll_s,
                                 stretch_clock) is None:
                return
            continue

        stretch = None
        fired = False
        count = 0
        recent = deque(maxlen=preroll_windows)
        if phase == "speaking":
            detector.reset()
            vad.reset()
            stretch = _Stretch(opened_at, None)
        try:
            while True:
                window = capture.read_window()
                count += 1
                every = poll_every_windows if stretch is not None else warm_poll_every_windows
                if count % every == 0:
                    phase = poll_phase()
                    if phase is None:
                        return
                    if is_paused():
                        phase = "idle"
                    if phase != "speaking" and stretch is not None:
                        break   # the stretch ended: close; PhaseTracker keeps it closed
                    if phase not in MIC_PHASES:
                        break
                    if phase == "speaking" and stretch is None:
                        # Everything captured so far, this window included, is from the
                        # warm time: drop it, so only audio captured after `speaking`
                        # was seen reaches the detector and the pre-roll.
                        drain = getattr(capture, "drain", None)
                        if drain is not None:
                            drain()
                        detector.reset()
                        vad.reset()
                        now = stretch_clock()
                        stretch = _Stretch(now, now - opened_at)
                        continue
                if stretch is None or window is None:
                    continue
                if stretch.first_window_at is None:
                    stretch.first_window_at = stretch_clock()
                if count <= warmup_windows:
                    continue
                if stretch.heard_at is None:
                    stretch.heard_at = stretch_clock()
                recent.append(window)
                prob = vad.prob(window)
                db = window_db(window)
                stretch.stats.add(db)
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
                                  poll_every_windows, clock, is_paused, preroll_discard)
                    break
        finally:
            try:
                if stretch is not None:
                    _log_stretch(stretch, stretch_clock() - stretch.asked_at, fired,
                                 _note_with_diagnostics(capture))
            finally:
                logger.debug("capture close: %s id=%#x", type(capture).__name__, id(capture))
                capture.close()

        # Don't reopen the mic for the utterance that is being cut.
        while fired:
            phase = poll_phase()
            if phase is None:
                return
            if phase != "speaking":
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


def auto_line(route: str) -> str:
    """auto (on: headphones), auto (off: speakers), auto (off: unknown)."""
    return f"auto ({'on' if route == 'headphones' else 'off'}: {route})"


class RouteGate:
    """auto: the mic may open only while the output route is headphones. The route is read
    at most once per ``every_s`` (a Windows read took ~7 ms, the first 41 ms; 2026-11-10,
    n=3), and each change is logged once."""

    def __init__(self, route=None, clock=time.monotonic, every_s: float = 1.0):
        self._route_fn = route or output_route
        self._clock, self._every = clock, every_s
        self._at, self._route = None, None

    def route(self) -> str:
        now = self._clock()
        if self._at is None or now - self._at >= self._every:
            route = self._route_fn()
            if route != self._route:
                logger.info("auto: route %s, barge-in %s", route,
                            "listening" if route == "headphones" else "off")
            self._at, self._route = now, route
        return self._route

    def closed(self) -> bool:
        return self.route() != "headphones"


def status_line() -> str:
    """on, off, auto (<on|off>: <route>) or cannot run: <reason>. What FELIX reads at
    voice start. On macOS, when it can run, a second line:
    ``route: <route>, capture: <capture or refused>``."""
    mode = barge_mode()
    if mode is None:
        return CANNOT_RUN
    first = auto_line(output_route()) if mode == "auto" else mode
    if _platform() != "darwin":
        return first
    route, capture = mac_capture_choice(echo_mode(trigger_settings().get(ECHO_SETTING)))
    return f"{first}\nroute: {route}, capture: {capture}"


def _preload_backend(backend) -> None:
    """Import sounddevice now: a cold import took 1063 ms on Windows (n=1, 2026-11-07),
    which the first reply's mic would otherwise wait for. Failure is left to the capture."""
    if backend != "sounddevice":
        return
    try:
        import sounddevice  # noqa: F401
    except Exception:  # noqa: BLE001 - OSError without PortAudio; the capture reports it
        pass


def log_level(raw) -> int:
    """VOICEMODE_DEBUG: DEBUG when true, 1, yes or on; INFO otherwise."""
    value = (raw or "").strip().strip('"').strip("'").lower()
    return logging.DEBUG if value in ("1", "true", "yes", "on") else logging.INFO


def main() -> int:
    if sys.argv[1:2] == ["status"]:
        print(status_line())
        return 0
    debug = os.environ.get("VOICEMODE_DEBUG")
    if debug is None:
        debug = read_env_file().get("VOICEMODE_DEBUG")
    logging.basicConfig(level=log_level(debug), format="%(asctime)s %(name)s %(levelname)s %(message)s")
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

    warm = warm_enabled(settings.get(WARM_SETTING))
    _preload_backend(backend)

    stop = threading.Event()
    install_stop_handlers(stop)

    path = control_socket_path()
    logger.info("started: gate=%.1f dBFS need=%d windows device=%s mic=%s warm=%s socket=%s",
                gate_db, need, device, backend, "on" if warm else "off", path)
    phase = PhaseTracker(lambda: _status(path), warm=warm)
    gate = RouteGate() if barge_mode() == "auto" else None
    if gate is not None:
        logger.info("auto: listening only while the output route is headphones (now %s)", gate.route())
    run(
        poll_phase=lambda: None if stop.is_set() else phase(),
        open_capture=lambda: open_capture(backend, device, echo=echo, vp_probe=vp_probe),
        vad=SileroVad(model),
        detector=BargeDetector(gate_db, need),
        send_barge=lambda: send_barge_in(path),
        poll_listen_started=lambda: _status(path).get("listen_started_at"),
        preroll_sink=lambda samples, start_t: write_preroll(preroll_path(), samples, start_t),
        preroll_discard=lambda: preroll_path().unlink(missing_ok=True),
        is_paused=(lambda: is_paused() or gate.closed()) if gate is not None else is_paused,
        on_fire=lambda: record_interruption(time.time()),
    )
    logger.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
