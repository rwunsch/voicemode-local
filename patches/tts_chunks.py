"""Speak a long reply in parts, one TTS request per part (voicemode-local, 2026-11-14).

Why: voice-mode sends the whole message as one TTS request. A local Kokoro server
generates the complete audio before it answers, so the request must finish inside the
TTS timeout. On a Windows laptop that generates 2.5-5x slower than real time (n of about
25 log lines), a 1,178-character reply (about 54 s of audio) took 134.7 s and the
request timed out at 90 s: the reply was never spoken.

With VOICEMODE_TTS_CHUNK_CHARS set to a positive number, simple_tts_failover splits the
text at sentence ends into parts of at most that many characters (a longer sentence at
commas, semicolons and dashes, then between words), and speaks the parts in order, each
through the normal failover. Each request stays small, so it ends well inside the
timeout, and the first part starts playing sooner. Unset, empty or 0 means off: the
text goes out as one request, exactly as before.

- A stop, barge-in, hold or skip (control_stopped / transport_interrupted on a part)
  ends the reply: the remaining parts are not spoken. Between parts nothing is playing,
  and voice-mode refuses a barge-in unless something is; so from the end of the first
  part to the end of the reply the speaking flag stays on, and a stop, hold or
  skip-forward that arrives while a later part is being generated ends the reply too
  (the request is cancelled after a short grace; the control state is left for
  converse to act on, as after a cut playback).
- A reply in parts has no single metrics["audio_path"] (the per-part files are in
  audio_paths), so "repeat" regenerates the whole reply instead of one part.
- Known limits: a skip-back during part N replays part N only, then listens (converse
  replays the newest history entry, and each part is its own entry); no voicemode-local
  or FELIX control sends skip-back. Multi-utterance `turns` use a different path and are
  not split. A request whose client gave up keeps the server busy until it is done.
  A cut while a later part is generated lands after a short grace (about 0.3 s for a
  hold or stop, in converse's push-to-talk wrapper; 0.5 s for a barge-in, GRACE_S here),
  because the speaking flag cannot tell the silent gap from a running player.
- The setting is at least 40 characters; a smaller number is raised to 40.
- A part that fails ends the reply too. Its error says which part failed and how many
  were already spoken, so nothing is spoken twice by a retry of the whole text.
- Parts are generated one after another, not ahead of playback: on a machine slower
  than real time there is a pause between parts while the next one is generated.

Standard library only. patch_tts_chunks.py renames upstream's simple_tts_failover to
_vml_simple_tts_failover_whole and puts a simple_tts_failover in its place that calls
speak_in_chunks.
"""
import logging
import os
import re

logger = logging.getLogger("voicemode")

SETTING = "VOICEMODE_TTS_CHUNK_CHARS"

# A sentence: from a non-space up to sentence-ending punctuation (with any closing
# quotes or brackets) followed by whitespace, or up to a line break, or to the end.
_SENTENCE = re.compile(r"\S.*?(?:[.!?…]+[\"'”’)\]]*(?=\s)|(?=\n)|$)", re.S)
# A clause boundary inside a long sentence: after , ; : or a dash, before whitespace.
_CLAUSE = re.compile(r"(?<=[,;:–—])\s+")
# A flag on a part's metrics that means "do not speak the rest".
STOP_FLAGS = ("control_stopped", "transport_interrupted")
# Smallest part size: below this, parts cut words apart and requests multiply.
MIN_CHARS = 40
# How often a later part's generation checks for a stop, hold or skip-forward, and how
# long it then lets a running player stop by itself before cancelling the request
# (the same pattern as push-to-talk's cut in converse).
POLL_S = 0.05
GRACE_S = 0.5


def chunk_chars() -> int:
    """The part size from VOICEMODE_TTS_CHUNK_CHARS; 0 (off) when unset or not a whole
    number above zero."""
    raw = os.environ.get(SETTING)
    if raw is None or not raw.strip():
        return 0
    try:
        value = int(raw.strip())
    except ValueError:
        value = -1
    if value < 0:
        logger.warning("%s=%r is not a whole number of characters; speaking replies in one part",
                       SETTING, raw)
        return 0
    if 0 < value < MIN_CHARS:
        logger.warning("%s=%r is below %d; using %d", SETTING, raw, MIN_CHARS, MIN_CHARS)
        return MIN_CHARS
    return value


def _words(text: str, limit: int) -> list:
    out, current = [], ""
    for word in text.split():
        while len(word) > limit:
            if current:
                out.append(current)
                current = ""
            out.append(word[:limit])
            word = word[limit:]
        if not word:
            continue
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= limit:
            current += " " + word
        else:
            out.append(current)
            current = word
    if current:
        out.append(current)
    return out


def _pack(pieces, limit: int, split_long) -> list:
    """Join pieces with single spaces into parts of at most `limit` characters; a piece
    longer than that is split by `split_long` and never joined to its neighbours."""
    out, current = [], ""
    for piece in pieces:
        piece = " ".join(piece.split())
        if not piece:
            continue
        if len(piece) > limit:
            if current:
                out.append(current)
                current = ""
            out.extend(split_long(piece))
        elif not current:
            current = piece
        elif len(current) + 1 + len(piece) <= limit:
            current += " " + piece
        else:
            out.append(current)
            current = piece
    if current:
        out.append(current)
    return out


def split_for_tts(text: str, limit: int) -> list:
    """The parts to speak, in order. One part, the text unchanged, when `limit` is 0 or
    less or the text already fits. Otherwise whitespace inside the text becomes single
    spaces; no word is lost, added or reordered."""
    if limit <= 0 or len(text) <= limit:
        return [text]

    def clauses(sentence):
        return _pack(_CLAUSE.split(sentence), limit, lambda clause: _words(clause, limit))

    return _pack(_SENTENCE.findall(text), limit, clauses) or [text]


def _combine(total: dict, metrics: dict) -> None:
    """Add one part's metrics: generation and playback add up, time to first audio stays
    the first part's, each part's audio_path goes into audio_paths, anything else (a stop
    flag) is the latest part's."""
    paths = total.setdefault("audio_paths", [])
    if metrics.get("audio_path"):
        paths.append(metrics["audio_path"])
    for key, value in metrics.items():
        if key == "audio_path" or (key == "ttfa" and "ttfa" in total):
            continue
        if key in ("generation", "playback") and isinstance(value, (int, float)):
            total[key] = total.get(key, 0.0) + value
        else:
            total[key] = value


def _control_state():
    """voice-mode's control state, or None outside a voice_mode package (unit tests)."""
    try:
        from .control_channel import get_control_state
    except ImportError:
        return None
    try:
        return get_control_state()
    except Exception:
        logger.debug("TTS parts: no control state", exc_info=True)
        return None


def _cut(control) -> bool:
    if control is None:
        return False
    snap = control.snapshot()
    return bool(snap.is_stopped or snap.is_skip_forward or getattr(snap, "is_holding", False))


def _set_speaking(control, value: bool) -> None:
    if control is not None and hasattr(control, "set_speaking"):
        control.set_speaking(value)


async def _part_unless_cut(whole, part, voice, model, kwargs, control):
    """One later part, ended early by a stop, hold or skip-forward. Returns the part's
    result, or None after a cut. A playing part stops by itself on that state (its
    playback loop checks it), so it gets GRACE_S before the request is cancelled."""
    import asyncio

    task = asyncio.ensure_future(whole(part, voice, model, **dict(kwargs)))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=POLL_S)
            if done:
                return task.result()
            if not _cut(control):
                continue
            done, _ = await asyncio.wait({task}, timeout=GRACE_S)
            if done:
                return task.result()
            task.cancel()
            await asyncio.wait({task})
            if not task.cancelled() and task.exception() is None:
                return task.result()  # it finished before the cancel landed
            return None
    except asyncio.CancelledError:
        task.cancel()
        raise


async def speak_in_chunks(whole, text, voice, model, kwargs, control_state=None):
    """simple_tts_failover in parts. `whole` is upstream's simple_tts_failover; it gets
    a fresh copy of kwargs for every part, because it pops ref_text from them.
    `control_state` is voice-mode's control state (looked up when not given)."""
    parts = split_for_tts(text, chunk_chars())
    if len(parts) == 1:
        return await whole(text, voice, model, **dict(kwargs))
    control = control_state if control_state is not None else _control_state()
    count = len(parts)
    logger.info("TTS: speaking %d characters in %d parts of at most %d (%s)",
                len(text), count, chunk_chars(), SETTING)
    total, first_config = {}, None
    try:
        for number, part in enumerate(parts, 1):
            if number == 1:
                result = await whole(part, voice, model, **dict(kwargs))
            else:
                if _cut(control):
                    logger.info("TTS: cut after part %d of %d; the rest is not spoken", number - 1, count)
                    break
                # Nothing plays while this part is generated; keep barge-in possible.
                _set_speaking(control, True)
                result = await _part_unless_cut(whole, part, voice, model, kwargs, control)
                if result is None:
                    logger.info("TTS: cut while part %d of %d was generated; the rest is not spoken",
                                number, count)
                    break
            success, metrics, config = result
            if not success:
                spoken = number - 1
                before = "1 part was" if spoken == 1 else f"{spoken} parts were"
                note = (f" (part {number} of {count}; {before} spoken before it, "
                        "and the rest of the reply was not sent)")
                for attempt in (config or {}).get("attempted_endpoints", []):
                    attempt["error"] = f"{attempt.get('error', '')}{note}"
                logger.error("TTS: part %d of %d failed; the rest of the reply was not sent", number, count)
                return False, None, config
            if first_config is None:
                first_config = config
            _combine(total, metrics or {})
            stop = next((flag for flag in STOP_FLAGS if (metrics or {}).get(flag)), None)
            if stop:
                logger.info("TTS: %s after part %d of %d; the rest is not spoken", stop, number, count)
                break
    finally:
        _set_speaking(control, False)
    return True, total, first_config
