"""Which language a local voice speaks, and the ISO code of a language name.

Stdlib only, no imports from voice_mode: copied into the package (manifest "copy" step) for
voice_fallback.py, the STT language patch and voice_pill.py, and loaded on its own by the tests.
The one voice-id -> language table: the pill imports it (it had its own, which drifted),
and its language names are what the pill's override holds ("English", "German", ...).

A voice id says its language: Piper's p_<code>_<name>, else Kokoro's first letter
(a/b English, e Spanish, f French, h Hindi, i Italian, j Japanese, p Portuguese,
z Chinese, d German). The local servers answer GET /v1/audio/voices as
{"data": [{id, language_name}]} (kokoro-onnx-server, piper-proxy) or {"voices": [ids]}
(Kokoro-FastAPI); voice_pairs reads both.
"""

KOKORO_LANGUAGES = {"a": "English", "b": "English", "e": "Spanish", "f": "French", "h": "Hindi",
                    "i": "Italian", "j": "Japanese", "p": "Portuguese", "z": "Chinese", "d": "German"}
PIPER_LANGUAGES = {"de": "German", "nl": "Dutch", "pl": "Polish", "ru": "Russian", "ko": "Korean",
                   "en": "English"}
ISO_CODES = {"English": "en", "German": "de", "Spanish": "es", "French": "fr", "Italian": "it",
             "Japanese": "ja", "Portuguese": "pt", "Chinese": "zh", "Hindi": "hi", "Dutch": "nl",
             "Polish": "pl", "Russian": "ru", "Korean": "ko"}


def language_from_id(voice):
    """A voice's language name from its id alone, or None when the id does not say."""
    if not isinstance(voice, str) or not voice:
        return None
    if voice.startswith("p_") and voice.count("_") >= 2:
        return PIPER_LANGUAGES.get(voice.split("_")[1])
    if len(voice) >= 3 and voice[1] in "fm" and voice[2] == "_":
        return KOKORO_LANGUAGES.get(voice[0])
    return None


def iso_code(name):
    """The ISO 639-1 code of a language name ("German" -> "de"), any case; None if unknown."""
    if not isinstance(name, str):
        return None
    wanted = name.strip().lower()
    return next((code for known, code in ISO_CODES.items() if known.lower() == wanted), None)


def voice_pairs(body):
    """[(voice id, language name)] from a /v1/audio/voices answer of either shape; a voice
    whose language is not known is left out, and anything else gives []."""
    pairs = []
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        for item in body["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            name = item.get("language_name")
            pairs.append((item["id"], name if isinstance(name, str) and name else language_from_id(item["id"])))
    elif isinstance(body, dict) and isinstance(body.get("voices"), list):
        pairs = [(v, language_from_id(v)) for v in body["voices"] if isinstance(v, str)]
    return [(voice, language) for voice, language in pairs if language]
