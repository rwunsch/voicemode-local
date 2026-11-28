#!/usr/bin/env python3
"""Patch voice_mode/tools/converse.py so a mic stream that fails to start is retried once
with silence detection, and the fixed-length fallback ends when the caller is cancelled.

Found 2026-11-18 22:24:53 (~/voicemode_debug.log): "Recording with VAD failed: Error starting
stream: Wait timed out [PaErrorCode -9987]", then "Falling back to fixed duration recording"
for 120 s with no silence detection. Upstream re-initialises PortAudio only for the errors in
its own list ('device unavailable', 'device disconnected', 'invalid device', 'unanticipated
host error', 'portaudio error'); a start timeout is not one, so the turn went straight to a
deaf 120 s recording, and ESC (stop_event) did not end it: the thread recorded all of it.

Inside record_audio_with_silence_detection (its ``_inner`` body when patch_voice_pill has
wrapped it), in the except block after the VAD stream failed:

1. A start failure ('wait timed out', 'paerrorcode -9987', 'error starting stream') that is
   not in upstream's list goes through upstream's own re-init code (sd._terminate();
   sd._initialize(); 0.5 s) and is retried ONCE with silence detection. The retry is marked
   in a thread-local, so a second start failure in it falls back as before: never a loop.
   Errors in upstream's list keep upstream's path unchanged. VOICEMODE_AUDIO_RECOVER=false
   turns the retry off (default true).
2. Both "Falling back to fixed duration recording" returns call _vml_fallback_record, which
   records as upstream's record_audio does, but a watcher stops that recording's stream
   (only one sd.rec started after the fallback began, never an earlier one) when stop_event
   is set, checked every 0.1 s; and VOICEMODE_FALLBACK_RECORD_MAX_S, when set to a number
   above 0, caps its length. Unset: max_duration, as upstream. Without a stop_event it is
   upstream's record_audio call exactly.

Helpers go in before ``def record_audio(``. Anchors (each must occur as stated, else ANCHOR
DRIFT, exit 1, nothing written): record_audio once in the file; the recording function once;
in it, upstream's recoverable-error check, the recursive retry line, and the two fallback
returns. Idempotent by marker. Keeps the file's line endings. Verified against voice-mode
8.12.0 pristine and after the whole manifest (2026-11-18). Stdlib only.

Usage: patch_audio_recover.py [<path-to-converse.py>]
"""
import re
import sys
from pathlib import Path

TAG = "(audio recover)"
MARKER = "voicemode-local audio recover"
FUNC = "def record_audio_with_silence_detection("
FUNC_INNER = "def record_audio_with_silence_detection_inner("
A_RECORD_AUDIO = "\ndef record_audio(duration: float) -> np.ndarray:\n"
A_CHECK = (
    "            error_str = str(e).lower()\n"
    "            if any(err in error_str for err in ['device unavailable', 'device disconnected', \n"
    "                                                 'invalid device', 'unanticipated host error',\n"
    "                                                 'portaudio error']):\n"
    "                logger.info(\"Audio device error detected - attempting to reinitialize audio system\")\n"
)
R_CHECK = (
    "            error_str = str(e).lower()\n"
    f"            # {MARKER}: a stream that failed to start (\"Wait timed out\",\n"
    "            # PaErrorCode -9987) takes the re-init below too, and is retried once with\n"
    "            # silence detection. VOICEMODE_AUDIO_RECOVER=false: upstream's list only.\n"
    "            _vml_recover = _vml_audio_recover_try(error_str)\n"
    "            if _vml_recover or any(err in error_str for err in ['device unavailable', 'device disconnected', \n"
    "                                                 'invalid device', 'unanticipated host error',\n"
    "                                                 'portaudio error']):\n"
    "                if _vml_recover:\n"
    "                    logger.info(\"Audio start timed out - re-initialising audio and retrying once\")\n"
    "                else:\n"
    "                    logger.info(\"Audio device error detected - attempting to reinitialize audio system\")\n"
)
A_RETRY = (
    "                    return record_audio_with_silence_detection(max_duration, "
    "disable_silence_detection, min_duration, vad_aggressiveness, stop_event)\n"
)
R_RETRY = (
    f"                    if _vml_recover:  # {MARKER}: once, marked so it cannot loop\n"
    "                        return _vml_audio_recover_retry(record_audio_with_silence_detection, "
    "max_duration, disable_silence_detection, min_duration, vad_aggressiveness, stop_event)\n"
    + A_RETRY
)
# The two error fallbacks; the webrtcvad-missing and silence-detection-off paths are
# intentional fixed-length recordings and stay as they are.
A_FALLBACK = re.compile(
    r"(logger\.info\(\"Falling back to fixed duration recording\"\)\n"
    r"[ \t]+# For fallback, assume speech is present since we can't detect\n[ \t]+)"
    r"return \(record_audio\(max_duration\), True\)\n")
R_FALLBACK = r"\1return (_vml_fallback_record(max_duration, stop_event), True)\n"
HELPERS = f'''
# {MARKER}: a mic stream that fails to start is re-initialised and retried
# once with silence detection; the fixed-length fallback ends when the caller is cancelled
# (stop_event) and VOICEMODE_FALLBACK_RECORD_MAX_S can cap it. See docs/audio-recover/README.md.
_VML_START_FAILURES = ("wait timed out", "paerrorcode -9987", "error starting stream")
_VML_UPSTREAM_DEVICE_ERRORS = ("device unavailable", "device disconnected", "invalid device",
                               "unanticipated host error", "portaudio error")
_vml_audio_recover_state = threading.local()


def _vml_audio_recover_try(error_str: str) -> bool:
    """True for a stream start failure upstream does not handle, with the switch on, when
    this recording has not retried yet."""
    try:
        if any(err in error_str for err in _VML_UPSTREAM_DEVICE_ERRORS):
            return False   # upstream's own path, unchanged
        if not any(err in error_str for err in _VML_START_FAILURES):
            return False
        if os.environ.get("VOICEMODE_AUDIO_RECOVER", "true").strip().lower() in ("false", "0", "no", "off"):
            return False
        if getattr(_vml_audio_recover_state, "retrying", False):
            logger.info("Audio start failed again after the retry - falling back")
            return False
        return True
    except Exception:
        return False


def _vml_audio_recover_retry(record, *args):
    """The one retry: a start failure inside it falls back instead of retrying again."""
    _vml_audio_recover_state.retrying = True
    try:
        return record(*args)
    finally:
        _vml_audio_recover_state.retrying = False


def _vml_fallback_seconds(max_duration):
    raw = os.environ.get("VOICEMODE_FALLBACK_RECORD_MAX_S", "").strip()
    if not raw:
        return max_duration
    try:
        cap = float(raw)
    except ValueError:
        logger.warning(f"VOICEMODE_FALLBACK_RECORD_MAX_S={{raw!r}} is not a number - ignored")
        return max_duration
    if not cap > 0 or cap == float("inf"):
        return max_duration
    if cap < max_duration:
        logger.info(f"Fixed duration fallback capped at {{cap:g}}s (VOICEMODE_FALLBACK_RECORD_MAX_S)")
        return cap
    return max_duration


def _vml_fallback_done():
    """Was Done (skip_forward) pressed? The fixed-length recording reads no control state of
    its own (Talos on FELIX #2104). Left set: converse consumes it after the recording."""
    try:
        from voice_mode.control_channel import get_control_state
        return bool(get_control_state().snapshot().is_skip_forward)
    except Exception:
        return False


def _vml_fallback_written(stopped, seen, total):
    """Frames sd.rec wrote before Done stopped its stream. Its buffer comes from np.empty, so
    the rest is whatever memory held and must not reach the transcription. sounddevice counts
    the frames written in its callback context (private, so guarded, and used only when it
    belongs to the stream that was stopped); else the time since the watcher first saw the
    stream, which started before that and so never counts past what was recorded."""
    stream, at = stopped
    cb = getattr(sd, "_last_callback", None)
    frame = getattr(cb, "frame", None)
    if getattr(cb, "stream", None) is stream and isinstance(frame, int) and 0 < frame <= total:
        return frame
    if seen:
        return max(1, min(total, int((at - seen[0]) * SAMPLE_RATE)))
    return 1


def _vml_fallback_record(max_duration, stop_event):
    """record_audio for the error fallback: capped by VOICEMODE_FALLBACK_RECORD_MAX_S, and
    its stream is stopped when stop_event is set or Done is pressed (checked every 0.1 s).
    After Done only what was recorded is returned, not the rest of the sd.rec buffer."""
    duration = _vml_fallback_seconds(max_duration)
    if stop_event is None:
        return record_audio(duration)
    if stop_event.is_set():
        logger.info("Fixed duration fallback skipped: caller was cancelled")
        return np.array([])
    try:
        before = sd.get_stream()   # an earlier sd.rec's stream is never touched
    except Exception:
        before = None
    done = threading.Event()
    seen = []      # when the watcher first saw this recording's stream (it started earlier)
    stopped = []   # the stream Done stopped

    def _watch():
        said = False
        while not done.wait(0.1):
            try:
                stream = sd.get_stream()
            except Exception:
                continue
            if stream is before or stream.closed:
                continue
            if not seen:
                seen.append(time.monotonic())
            cancelled = stop_event.is_set()
            if not cancelled and not _vml_fallback_done():
                continue
            try:
                if not said:
                    if cancelled:
                        logger.info("Fixed duration fallback recording stopped: caller was cancelled")
                    else:
                        logger.info("Fixed duration fallback recording ended by skip_forward (Done)")
                        stopped.append((stream, time.monotonic()))
                    said = True
                stream.stop()
            except Exception:
                pass

    threading.Thread(target=_watch, name="vml-fallback-stop", daemon=True).start()
    try:
        audio = record_audio(duration)
    finally:
        done.set()
    if stopped and len(audio):
        audio = audio[:_vml_fallback_written(stopped[0], seen, len(audio))]
    return audio

'''


def _read(target: Path):
    text = target.read_bytes().decode("utf-8")
    return text.replace("\r\n", "\n"), "\r\n" in text


def _write(target: Path, text: str, crlf: bool) -> None:
    target.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def _drift(target: Path, what: str, count: int, expected: int = 1) -> int:
    print(f"[patch_audio_recover] ANCHOR DRIFT {TAG}: {what} matched {count} times "
          f"(expected exactly {expected}) in {target}. Upstream voice-mode has likely changed: "
          f"update the anchors in patches/patch_audio_recover.py.", file=sys.stderr)
    return 1


# The function ends where the next top-level statement starts.
_NEXT_TOP = re.compile(r"\n(?=[^\s#\n])")


def apply(target: Path) -> int:
    src, crlf = _read(target)
    if MARKER in src:
        print(f"[patch_audio_recover] {target}: already patched {TAG} -- skipping")
        return 0
    if src.count(A_RECORD_AUDIO) != 1:
        return _drift(target, "def record_audio(duration: float)", src.count(A_RECORD_AUDIO))
    # patch_voice_pill moves upstream's body into ..._inner behind a wrapper: patch that one.
    func = FUNC_INNER if FUNC_INNER in src else FUNC
    if src.count(func) != 1:
        return _drift(target, func[4:-1], src.count(func))
    start = src.index(func)
    nxt = _NEXT_TOP.search(src, start + len(func))
    end = nxt.start() + 1 if nxt else len(src)
    body = src[start:end]
    if body.count(A_CHECK) != 1:
        return _drift(target, "the recoverable device-error check", body.count(A_CHECK))
    if body.count(A_RETRY) != 1:
        return _drift(target, "the re-init retry line", body.count(A_RETRY))
    fallbacks = len(A_FALLBACK.findall(body))
    if fallbacks != 2:
        return _drift(target, "the fixed duration fallback return", fallbacks, 2)
    body = body.replace(A_CHECK, R_CHECK).replace(A_RETRY, R_RETRY)
    body = A_FALLBACK.sub(R_FALLBACK, body)
    out = src[:start] + body + src[end:]
    at = out.index(A_RECORD_AUDIO) + 1
    out = out[:at] + HELPERS.lstrip("\n") + "\n" + out[at:]
    compile(out, str(target), "exec")  # syntax safety net before writing
    _write(target, out, crlf)
    print(f"[patch_audio_recover] patched {target} {TAG}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "tools" / "converse.py"
    sys.exit(apply(target))
