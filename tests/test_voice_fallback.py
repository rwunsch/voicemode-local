"""patches/voice_languages.py and patches/voice_fallback.py: an uninstalled voice is spoken
with an installed voice of the same language, never a 500 (Stephan, 2026-11-16: FELIX's chat
asked for a voice that was not installed and its first reply failed with HTTP 500).

Only local servers are asked which voices they have, and OpenAI is never the fallback (no
silent cloud substitution). Any failure that is not "unknown voice" keeps today's result."""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PATCHES = Path(__file__).resolve().parent.parent / "patches"
if str(PATCHES) not in sys.path:
    sys.path.insert(0, str(PATCHES))

import voice_fallback as vf  # noqa: E402
import voice_languages as vl  # noqa: E402


# -- languages --------------------------------------------------------------------------

@pytest.mark.parametrize("voice,language", [
    ("af_sky", "English"), ("bm_george", "English"), ("ef_dora", "Spanish"), ("ff_siwis", "French"),
    ("hf_alpha", "Hindi"), ("im_nicola", "Italian"), ("jm_kumo", "Japanese"), ("pf_dora", "Portuguese"),
    ("zf_xiaobei", "Chinese"), ("dm_martin", "German"), ("p_de_thorsten", "German"), ("p_nl_nathalie", "Dutch"),
    ("p_pl_gosia", "Polish"), ("p_ru_dmitri", "Russian"), ("p_ko_hana", "Korean"),
    ("nova", None), ("", None), (None, None), ("xf_x", None),
])
def test_language_from_a_voice_id(voice, language):
    assert vl.language_from_id(voice) == language


@pytest.mark.parametrize("name,code", [
    ("English", "en"), ("German", "de"), ("Spanish", "es"), ("French", "fr"), ("Italian", "it"),
    ("Japanese", "ja"), ("Portuguese", "pt"), ("Chinese", "zh"), ("Hindi", "hi"), ("Dutch", "nl"),
    ("Polish", "pl"), ("Russian", "ru"), ("Korean", "ko"), ("german", "de"), (" English ", "en"),
    ("Klingon", None), ("", None), (None, None),
])
def test_iso_code_of_a_language_name(name, code):
    assert vl.iso_code(name) == code


def test_voice_pairs_reads_both_answer_shapes():
    assert vl.voice_pairs({"data": [{"id": "af_sky", "language_name": "English"},
                                    {"id": "p_de_eva", "language_name": "German"},
                                    {"id": "odd"}, "junk"]}) == [("af_sky", "English"), ("p_de_eva", "German")]
    assert vl.voice_pairs({"voices": ["af_sky", "ef_dora", "zz"]}) == [("af_sky", "English"), ("ef_dora", "Spanish")]
    # The Piper proxy answers both keys, "voices" holding dicts: "data" wins.
    assert vl.voice_pairs({"data": [{"id": "p_de_eva", "language_name": "German"}],
                           "voices": [{"id": "p_de_eva"}]}) == [("p_de_eva", "German")]
    assert vl.voice_pairs(["af_sky"]) == [] and vl.voice_pairs(None) == []


# -- what counts as "unknown voice" ------------------------------------------------------

LOCAL_K, LOCAL_P, CLOUD = "http://127.0.0.1:8880/v1", "http://localhost:8881/v1", "https://api.openai.com/v1"


def _local(url):
    return "127.0.0.1" in url or "localhost" in url


def _attempt(base, error, voice="af_nobody"):
    return {"endpoint": f"{base}/audio/speech", "provider": "kokoro", "voice": voice, "model": "tts-1",
            "error": error}


@pytest.mark.parametrize("error", [
    "Error code: 404 - {'error': \"Voice 'af_nobody' not found\"}",                 # our kokoro-onnx server
    "Error code: 404 - <p>Message: Voice &#x27;af_nobody&#x27; not found.</p>",     # piper-proxy send_error
    "Error code: 400 - {'detail': {'error': 'validation_error', 'message': \"Voice 'af_nobody' not found. "
    "Available voices: af_sky\"}}",                                                 # Kokoro-FastAPI
    "Error code: 500 - {'error': \"'af_nobody'\"}",                                 # kokoro-onnx-server before 404
    "Unknown voice: af_nobody",
])
def test_unknown_voice_errors_are_recognised(error):
    assert vf.is_unknown_voice_error(error, "af_nobody")


@pytest.mark.parametrize("error", [
    "Connection error.", "Request timed out.", "Error code: 500 - {'error': 'CUDA out of memory'}",
    "Error code: 401 - invalid api key", "", None,
    "Error code: 500 - {'error': \"'af_sky'\"}",   # another voice's KeyError is not ours
])
def test_other_errors_are_not(error):
    assert not vf.is_unknown_voice_error(error, "af_nobody")


def test_unknown_everywhere_needs_every_local_endpoint_to_say_so():
    unknown = "Error code: 404 - {'error': \"Voice 'af_nobody' not found\"}"
    both = {"attempted_endpoints": [_attempt(LOCAL_K, unknown), _attempt(LOCAL_P, "Error code: 404 - Voice "
                                                                         "&#x27;af_nobody&#x27; not found"),
                                    _attempt(CLOUD, "Error code: 400 - invalid voice")]}
    assert vf.unknown_everywhere(both, "af_nobody", _local)
    one_down = {"attempted_endpoints": [_attempt(LOCAL_K, unknown), _attempt(LOCAL_P, "Connection error.")]}
    assert not vf.unknown_everywhere(one_down, "af_nobody", _local)
    cloud_only = {"attempted_endpoints": [_attempt(CLOUD, unknown)]}
    assert not vf.unknown_everywhere(cloud_only, "af_nobody", _local)
    assert not vf.unknown_everywhere({}, "af_nobody", _local)
    assert not vf.unknown_everywhere(None, "af_nobody", _local)


# -- choosing the fallback ---------------------------------------------------------------

INSTALLED = [("af_sky", "English"), ("bm_george", "English"), ("ef_dora", "Spanish"), ("dm_martin", "German"),
             ("p_de_thorsten", "German")]


@pytest.mark.parametrize("voice,configured,expected", [
    ("af_nobody", [], "af_sky"),                       # same language, first installed
    ("bf_nobody", ["bm_george", "af_sky"], "bm_george"),  # a configured voice of that language first
    ("p_de_nobody", [], "dm_martin"),                  # German from Piper's id
    ("em_nobody", ["af_sky"], "ef_dora"),
    ("jm_nobody", ["af_sky", "ef_dora"], "af_sky"),    # no Japanese: the first VOICEMODE_VOICES entry
    ("nova", ["bm_george"], "bm_george"),              # unknown language: the first configured
    ("jm_nobody", [], None),                           # nothing to fall back to
])
def test_choose(voice, configured, expected):
    installed = [pair for pair in INSTALLED if pair[0] != voice]
    assert vf.choose(voice, installed, configured) == expected


def test_never_fall_back_to_the_same_voice():
    assert vf.choose("af_sky", [("af_sky", "English")], ["af_sky"]) is None


# -- asking the local servers ------------------------------------------------------------

class _Voices(BaseHTTPRequestHandler):
    answers = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = self.answers.get(self.server.server_address[1])
        if body is None or self.path != "/v1/audio/voices":
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def servers():
    started = []

    def start(body):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Voices)
        _Voices.answers[httpd.server_address[1]] = body
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/v1"

    yield start
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


def test_installed_voices_asks_local_servers_only(servers):
    kokoro = servers({"voices": ["af_sky", "ef_dora"]})
    piper = servers({"object": "list", "data": [{"id": "p_de_eva", "language_name": "German"}],
                     "voices": [{"id": "p_de_eva"}]})
    asked = []
    real = vf._fetch_json

    def fetch(url):
        asked.append(url)
        return real(url)

    pairs = vf.installed_voices([kokoro, "http://127.0.0.1:1/v1", piper, CLOUD], _local, fetch=fetch)
    assert pairs == [("af_sky", "English"), ("ef_dora", "Spanish"), ("p_de_eva", "German")]
    assert all("openai" not in url for url in asked)


# -- the whole retry ---------------------------------------------------------------------

def _failure(voice, error="Error code: 404 - {'error': \"Voice '%s' not found\"}"):
    attempts = [_attempt(LOCAL_K, error % voice if "%s" in error else error, voice),
                _attempt(CLOUD, "Error code: 400 - invalid voice", voice)]
    return False, None, {"error_type": "all_providers_failed", "attempted_endpoints": attempts}


def _speaker(installed=("af_sky", "bm_george"), error=None):
    calls = []

    async def call(text, voice, model=None, **kwargs):
        calls.append((text, voice, model, kwargs))
        if voice in installed:
            return True, {"ttfa": 0.1}, {"base_url": LOCAL_K, "voice": voice, "provider": "kokoro"}
        return _failure(voice, error) if error else _failure(voice)

    return call, calls


def _run(call, voice, *, configured=("af_sky",), installed=INSTALLED):
    async def go():
        with vf.notes_scope() as notes:
            result = await vf.with_fallback(call, "Hello", voice, None, {"speed": 1.0},
                                            base_urls=[LOCAL_K, CLOUD], configured=list(configured),
                                            is_local=_local, installed=lambda: list(installed))
        return result, notes
    return asyncio.run(go())


def test_an_unknown_voice_is_spoken_with_an_installed_one_of_its_language():
    call, calls = _speaker()
    (ok, metrics, config), notes = _run(call, "bf_nobody", configured=["bm_george", "af_sky"])
    assert ok and config["voice"] == "bm_george"
    assert [c[1] for c in calls] == ["bf_nobody", "bm_george"]
    assert calls[1][3] == {"speed": 1.0}                      # the same request, another voice
    assert notes == ["Voice bf_nobody is not installed here; spoke with bm_george."]
    assert config["requested_voice"] == "bf_nobody"
    assert config["voice_fallback_note"] == notes[0]


def test_a_working_voice_is_called_once():
    call, calls = _speaker()
    (ok, _, config), notes = _run(call, "af_sky")
    assert ok and [c[1] for c in calls] == ["af_sky"] and notes == []


def test_another_failure_keeps_todays_result():
    call, calls = _speaker(error="Connection error.")
    result, notes = _run(call, "bf_nobody")
    assert result[0] is False and result[2]["error_type"] == "all_providers_failed"
    assert [c[1] for c in calls] == ["bf_nobody"] and notes == []


def test_nothing_to_fall_back_to_keeps_todays_result():
    call, calls = _speaker()
    result, notes = _run(call, "jm_nobody", configured=[], installed=[("af_sky", "English")])
    assert result[0] is False and len(calls) == 1 and notes == []


def test_the_fallback_is_tried_once_and_a_second_failure_is_returned():
    call, calls = _speaker(installed=())
    result, notes = _run(call, "af_nobody")
    assert result[0] is False and [c[1] for c in calls] == ["af_nobody", "af_sky"] and notes == []


def test_a_five_tuple_synthesis_result_works_too():
    calls = []

    async def synth(text, voice, model=None, **kwargs):
        calls.append(voice)
        if voice == "af_sky":
            return True, [0.0], 24000, {}, {"voice": voice}
        failure = _failure(voice)
        return False, None, None, None, failure[2]

    (ok, samples, rate, _, config), notes = _run(synth, "af_nobody")
    assert ok and rate == 24000 and config["voice"] == "af_sky" and calls == ["af_nobody", "af_sky"]
    assert notes == ["Voice af_nobody is not installed here; spoke with af_sky."]


def test_without_a_scope_the_note_is_only_logged(caplog):
    call, _ = _speaker()

    async def go():
        return await vf.with_fallback(call, "Hi", "af_nobody", None, {}, base_urls=[LOCAL_K],
                                      configured=["af_sky"], is_local=_local,
                                      installed=lambda: list(INSTALLED))
    import logging
    with caplog.at_level(logging.WARNING, logger="voicemode"):
        ok, _, config = asyncio.run(go())
    assert ok and "spoke with af_sky" in caplog.text


def test_a_fallback_is_never_a_cloud_voice():
    """Only local servers are asked, and only a voice they have is chosen: a configured cloud
    voice (nova) is skipped, so the retry can never become OpenAI speech."""
    assert vf.choose("af_nobody", [], ["nova"]) is None
    assert vf.choose("jm_nobody", [("af_sky", "English")], ["nova", "af_sky"]) == "af_sky"
    assert vf.installed_voices([CLOUD], _local, fetch=lambda url: pytest.fail("asked the cloud")) == []


def test_suffix():
    assert vf.suffix([]) == ""
    assert vf.suffix(["A.", "B."]) == " | A. B."
