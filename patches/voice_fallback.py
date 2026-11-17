"""An uninstalled voice is spoken with an installed voice of the same language, never a 500.

Why (Stephan, 2026-11-16): FELIX's chat asked for a voice that was not installed and its first
reply failed with HTTP 500. kokoro_onnx looks a voice up with voices[name], so the server
answered the KeyError as a bare 500 (kokoro-onnx-server.py now answers 404 "Voice 'x' not
found"); every local server rejected the voice and the turn was lost.

What: patch_voice_fallback.py wraps simple_tts_failover and simple_tts_synthesize with
with_fallback. When the request failed and EVERY local endpoint rejected the voice as unknown,
the local servers are asked which voices they have (GET /v1/audio/voices), and the request is
sent once more with, in this order:
  1. the first VOICEMODE_VOICES entry they have that speaks the requested voice's language,
  2. the first voice they have that speaks it,
  3. the first VOICEMODE_VOICES entry they have.
Only a voice a local server has is ever chosen, and only local servers are asked: OpenAI is
never the fallback (no silent cloud substitution). Any other failure, or nothing to fall back
to, returns today's result unchanged. A success records "Voice X is not installed here; spoke
with Y." in the converse call's notes (notes_scope), which converse adds to its result.

Stdlib only; copied into voice_mode (manifest "copy" step), loaded on its own by the tests.
"""
import asyncio
import contextlib
import contextvars
import json
import logging
import re
import urllib.request

try:
    from .voice_languages import language_from_id, voice_pairs
except ImportError:  # loaded on its own (tests): voice_languages.py is beside this file
    from voice_languages import language_from_id, voice_pairs

logger = logging.getLogger("voicemode")

VOICES_TIMEOUT_S = 2.0
NOTES = contextvars.ContextVar("vml_voice_fallback_notes", default=None)

_UNKNOWN = re.compile(r"voice\b.{0,80}?\bnot\s+found|unknown\s+voice|no\s+such\s+voice", re.I | re.S)


def is_unknown_voice_error(error, voice):
    """The error text says the server does not have the voice: "Voice 'x' not found" (our
    Kokoro server, Piper's HTML-escaped reply, Kokoro-FastAPI), "unknown voice", or the bare
    KeyError of a kokoro-onnx-server from before its 404 (a 500 whose error is 'voice')."""
    if not isinstance(error, str) or not error:
        return False
    if _UNKNOWN.search(error):
        return True
    return bool(voice) and re.search(r"\b500\b", error) is not None and (
        f"\"'{voice}'\"" in error or f"'\\'{voice}\\''" in error or f"\"error\": \"'{voice}'\"" in error)


def _base(endpoint):
    return endpoint[:-len("/audio/speech")] if endpoint.endswith("/audio/speech") else endpoint


def unknown_everywhere(error_config, voice, is_local):
    """Every local endpoint that was tried rejected the voice as unknown (at least one was)."""
    if not isinstance(error_config, dict):
        return False
    attempts = [a for a in error_config.get("attempted_endpoints") or [] if isinstance(a, dict)]
    local = [a for a in attempts if is_local(_base(str(a.get("endpoint", ""))))]
    return bool(local) and all(is_unknown_voice_error(a.get("error"), voice) for a in local)


def _fetch_json(url):
    """The JSON a local server answers at url, without any proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=VOICES_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def installed_voices(base_urls, is_local, fetch=None):
    """[(voice id, language)] every local TTS server has, in endpoint order. A server that is
    down or answers oddly is skipped; a non-local endpoint is never asked."""
    fetch = fetch or _fetch_json
    pairs = []
    for base in base_urls or []:
        if not isinstance(base, str) or not is_local(base):
            continue
        try:
            found = voice_pairs(fetch(base.rstrip("/") + "/audio/voices"))
        except Exception as error:  # noqa: BLE001 - one server's trouble must not stop the rest
            logger.info(f"voice fallback: could not list the voices at {base}: {error}")
            continue
        pairs += [pair for pair in found if pair not in pairs]
    return pairs


def choose(voice, installed, configured):
    """The fallback for voice among the installed (voice, language) pairs, or None."""
    have = [v for v, _ in installed if v != voice]
    language = language_from_id(voice)
    if language:
        same = [v for v, lang in installed if lang == language and v != voice]
        for candidate in configured or []:
            if candidate in same:
                return candidate
        if same:
            return same[0]
    for candidate in configured or []:
        if candidate in have:
            return candidate
    return None


def note(requested, used):
    return f"Voice {requested} is not installed here; spoke with {used}."


@contextlib.contextmanager
def notes_scope():
    """Collects the notes of one converse call (tasks it starts share the list)."""
    notes = []
    token = NOTES.set(notes)
    try:
        yield notes
    finally:
        NOTES.reset(token)


def suffix(notes):
    return " | " + " ".join(notes) if notes else ""


async def with_fallback(call, text, voice, model, kwargs, *, base_urls, configured, is_local,
                        installed=None):
    """call(text, voice, model, **kwargs) -> a tuple whose first item is success and whose last
    is the config (the error config on failure). See the module docstring."""
    first = await call(text, voice, model, **kwargs)
    if first[0] or not voice or not unknown_everywhere(first[-1], voice, is_local):
        return first
    try:
        if installed is None:
            pairs = await asyncio.to_thread(installed_voices, base_urls, is_local)
        else:
            pairs = installed()
        fallback = choose(voice, pairs, configured)
    except Exception:  # noqa: BLE001 - the fallback must never make a failure worse
        logger.warning("voice fallback: could not choose a voice", exc_info=True)
        return first
    if not fallback:
        logger.warning(f"voice fallback: voice {voice} is not installed and no installed voice can stand in")
        return first
    logger.warning(f"voice fallback: voice {voice} is not installed here; trying {fallback}")
    second = await call(text, fallback, model, **kwargs)
    if not second[0]:
        return second
    said = note(voice, fallback)
    logger.warning(f"voice fallback: {said}")
    notes = NOTES.get()
    if notes is not None:
        notes.append(said)
    config = second[-1]
    if isinstance(config, dict):
        config["requested_voice"] = voice
        config["voice_fallback_note"] = said
    return second
