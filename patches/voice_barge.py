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

Limit: only the buffered and turns[] players report ``speaking``, so with
VOICEMODE_STREAMING_ENABLED=true this stays inert.

Enable with ``voicemode-switch barge on``. Linux/WSL only for now (needs parec).
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
        _exchange, _status, barge_control, control_socket_path, is_paused, record_interruption)
except ImportError:  # run straight from the repo, not the installed package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from voice_pill_state import (  # noqa: F401
        _exchange, _status, barge_control, control_socket_path, is_paused, record_interruption)

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


def is_speaking(path: Path) -> bool:
    """True only if a server says TTS is playing."""
    return bool(_status(path).get("speaking"))


def preroll_path() -> Path:
    return Path.home() / ".voicemode" / "barge_preroll.bin"


def write_preroll(path: Path, samples: np.ndarray, start_t: float) -> None:
    """Hand audio to the server: atomic, and private because it holds voice."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    tmp = Path(f"{path}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(PREROLL_HEADER.pack(start_t) + pcm)
    os.replace(tmp, path)


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
    try:
        raw = path.read_bytes()
        path.unlink()
    except OSError:
        return audio
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
        is_paused=lambda: False, on_fire=None) -> None:
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
            capture.close()

        # Don't reopen the mic for the utterance that is being cut.
        while fired:
            speaking = poll_speaking()
            if speaking is None:
                return
            if not speaking:
                break
            sleep(idle_poll_s)


def _read_env_file() -> dict:
    env = {}
    path = Path.home() / ".voicemode" / "voicemode.env"
    try:
        for line in path.read_text().splitlines():
            if line.startswith("VOICEMODE_VOICE_BARGE") and "=" in line:
                key, value = line.split("=", 1)
                env[key] = value.strip()
    except OSError:
        pass
    return env


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = {**_read_env_file(), **{k: v for k, v in os.environ.items() if k.startswith("VOICEMODE_VOICE_BARGE")}}
    if shutil.which("parec") is None:
        logger.error("parec not found; voice barge-in needs PulseAudio or PipeWire tools")
        return 1
    if len(sys.argv) < 2:
        logger.error("usage: python -m voice_mode.voice_barge <silero_vad.onnx>")
        return 2
    model = Path(sys.argv[1])
    if not model.is_file():
        logger.error("Silero model not found at %s; run: voicemode-switch barge on", model)
        return 1

    gate_db = float(settings.get("VOICEMODE_VOICE_BARGE_GATE_DB", DEFAULT_GATE_DB))
    min_ms = float(settings.get("VOICEMODE_VOICE_BARGE_MIN_SPEECH_MS", DEFAULT_MIN_SPEECH_MS))
    device = settings.get("VOICEMODE_VOICE_BARGE_DEVICE", "@DEFAULT_SOURCE@")
    need = max(1, round(min_ms / (WINDOW_S * 1000)))

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    path = control_socket_path()
    logger.info("started: gate=%.1f dBFS need=%d windows device=%s socket=%s", gate_db, need, device, path)
    run(
        poll_speaking=lambda: None if stop.is_set() else is_speaking(path),
        open_capture=lambda: ParecCapture(device),
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
