"""Numbers about one recorded turn, without audio or text (FELIX #2107).

The recording loop (patch_turn_stats.py) feeds every 30 ms frame here with webrtcvad's
decision and the frame's level, and names how the recording ended. The RECORDING_END event
then carries fixed fields, so that "listening did not end" can be told apart from a turn that
was ended by Done, push-to-talk, the cap or a stop, and so that a noisy room shows up as a
level instead of a guess.

Fields (always present; null when unknown, e.g. a recording without silence detection):
  end_reason          silence | done | ptt_release | cap | no_speech | stop | pause |
                      skip_back | cancel | stall | device_error | error | no_vad
  frames              frames seen (30 ms each)
  speech_share        share of frames judged speech after the first speech frame (0..1)
  pause_resets        pauses of >= 300 ms that a speech frame cut short (the turn went on)
  longest_pause_ms    the longest run of non-speech frames after the first speech
  quiet_dbfs          median level of the non-speech frames, dBFS (about -90 quiet .. 0 loud)
  silence_needed_ms   the pause that would have ended the turn (the threshold in force)

Nothing here records or keeps audio; only these numbers leave the loop.
"""
import math
import threading

REASONS = ("silence", "done", "ptt_release", "cap", "no_speech", "stop", "pause",
           "skip_back", "cancel", "stall", "device_error", "error", "no_vad")
FRAME_MS = 30
RESET_MIN_FRAMES = 10          # a pause of >= 300 ms
MAX_QUIET_SAMPLES = 20000      # 600 s of frames; the cap is 420 s
FIELDS = ("end_reason", "frames", "speech_share", "pause_resets", "longest_pause_ms",
          "quiet_dbfs", "silence_needed_ms")

_lock = threading.Lock()
_current = None
_last = None


class TurnStats:
    def __init__(self, silence_needed_ms=None):
        self.frames = 0
        self.after_first = 0
        self.speech_after_first = 0
        self.active = False
        self.run = 0
        self.longest = 0
        self.resets = 0
        self.quiet = []
        self.reason = None
        self.silence_needed_ms = silence_needed_ms

    def frame(self, is_speech, rms):
        self.frames += 1
        if not is_speech and len(self.quiet) < MAX_QUIET_SAMPLES:
            self.quiet.append(_dbfs(rms))
        if not self.active:
            if is_speech:
                self.active = True
                self.after_first = self.speech_after_first = 1
            return
        self.after_first += 1
        if is_speech:
            self.speech_after_first += 1
            if self.run >= RESET_MIN_FRAMES:
                self.resets += 1
            self.run = 0
        else:
            self.run += 1
            self.longest = max(self.longest, self.run)

    def end(self, reason):
        """The first reason given wins: the branch that broke the loop knows best."""
        if self.reason is None and reason in REASONS:
            self.reason = reason

    def fields(self):
        q = sorted(self.quiet)
        return {
            "end_reason": self.reason,
            "frames": self.frames,
            "speech_share": round(self.speech_after_first / self.after_first, 3) if self.after_first else 0.0,
            "pause_resets": self.resets,
            "longest_pause_ms": self.longest * FRAME_MS,
            "quiet_dbfs": round(q[len(q) // 2], 1) if q else None,
            "silence_needed_ms": self.silence_needed_ms,
        }


def _dbfs(rms):
    try:
        rms = float(rms)
    except (TypeError, ValueError):
        return -90.0
    if not math.isfinite(rms) or rms <= 1.0:
        return -90.0
    return max(-90.0, 20 * math.log10(rms / 32768.0))


def start(silence_needed_ms=None):
    """A new recording begins. Returns its TurnStats (also kept as the current one)."""
    global _current
    with _lock:
        _current = TurnStats(silence_needed_ms)
        return _current


def finish(stats, reason=None):
    """The recording loop has returned; keep its numbers for the RECORDING_END event."""
    global _current, _last
    with _lock:
        if stats is None:
            return
        if reason:
            stats.end(reason)
        _last = stats
        if _current is stats:
            _current = None


def no_vad():
    """A recording without silence detection (disabled, or webrtcvad missing)."""
    s = TurnStats()
    s.end("no_vad")
    finish(s)


def event_fields():
    """The fields for RECORDING_END, consumed once: a later turn never reports these numbers."""
    global _last
    with _lock:
        stats, _last = _last, None
    if stats is None:
        return {k: None for k in FIELDS}
    return stats.fields()
