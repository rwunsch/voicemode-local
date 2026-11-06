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
through sounddevice (PortAudio) on Windows and macOS.
Installed into voice_mode/voice_barge.py by patches/apply.sh.
"""
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


def open_capture(backend: str, device: str):
    """The mic for one stretch of speech, by the backend capture_backend() chose."""
    if backend == "parec":
        return ParecCapture(device)
    return SoundDeviceCapture(device)


class StretchStats:
    """Levels of one speaking stretch, kept as a list of floats: no audio, no per-window log."""

    def __init__(self):
        self.dbs: list = []

    def add(self, db: float) -> None:
        self.dbs.append(db)

    def line(self, seconds: float, fired: bool) -> str:
        if self.dbs:
            peak = f"{max(self.dbs):.1f} dBFS"
            p90 = f"{float(np.percentile(self.dbs, 90)):.1f} dBFS"
        else:
            peak = p90 = "n/a"
        return (f"stretch: {seconds:.1f} s, windows {len(self.dbs)}, max {peak}, "
                f"p90 {p90}, fired {'yes' if fired else 'no'}")


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
        capture = open_capture()
        fired = False
        count = 0
        recent = deque(maxlen=preroll_windows)
        stats = StretchStats()
        opened_at = stretch_clock()
        try:
            while True:
                window = capture.read_window()
                count += 1
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
                logger.info("%s", stats.line(stretch_clock() - opened_at, fired))
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
    """on, off or cannot run: <reason>. What FELIX reads at voice start."""
    state = barge_state()
    if state is None:
        return CANNOT_RUN
    return "on" if state else "off"


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

    stop = threading.Event()
    install_stop_handlers(stop)

    path = control_socket_path()
    logger.info("started: gate=%.1f dBFS need=%d windows device=%s mic=%s socket=%s",
                gate_db, need, device, backend, path)
    run(
        poll_speaking=lambda: None if stop.is_set() else is_speaking(path),
        open_capture=lambda: open_capture(backend, device),
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
