#!/usr/bin/env python3
"""Demonstrate/verify TTS failover routing without playing audio.

For each test voice, walk the configured VOICEMODE_TTS_BASE_URLS in priority
order and show which endpoint actually serves it (first HTTP 2xx). This mirrors
what voice-mode's simple_tts_failover does. Use --down to simulate an engine
being offline and watch the fallback move to the next endpoint.

Usage:
  voicemode-test-tts.py                      # test af_sky / p_de_thorsten / nova
  voicemode-test-tts.py af_sky p_de_thorsten # test specific voices
  voicemode-test-tts.py --down kokoro        # simulate Kokoro offline
  voicemode-test-tts.py --down kokoro,piper  # simulate both locals offline
"""
import json
import os
import sys
import time
import urllib.request

DEFAULT_TTS = ["http://127.0.0.1:8880/v1",
               "http://127.0.0.1:8881/v1",
               "https://api.openai.com/v1"]
DEFAULT_VOICES = [("af_sky", "Kokoro"), ("p_de_thorsten", "Piper"), ("nova", "OpenAI")]
ENGINE_PORT = {"kokoro": "8880", "piper": "8881", "whisper": "2022", "openai": "openai.com"}


def configured_tts_urls():
    """Read the list voice-mode would actually use (env > voicemode.env > .claude.json)."""
    v = os.environ.get("VOICEMODE_TTS_BASE_URLS")
    if not v:
        env_file = os.path.expanduser("~/.voicemode/voicemode.env")
        if os.path.exists(env_file):
            for line in open(env_file):
                line = line.strip()
                if line.startswith("VOICEMODE_TTS_BASE_URLS="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not v:
        try:
            e = json.load(open(os.path.expanduser("~/.claude.json")))
            v = e["mcpServers"]["voicemode"]["env"].get("VOICEMODE_TTS_BASE_URLS")
        except Exception:
            pass
    return [u.strip() for u in v.split(",")] if v else list(DEFAULT_TTS)


def engine_name(url):
    if "8880" in url: return "Kokoro"
    if "8881" in url: return "Piper"
    if "openai.com" in url: return "OpenAI"
    return url


def try_endpoint(url, voice, timeout=20):
    body = json.dumps({"model": "tts-1", "input": "test", "voice": voice,
                       "response_format": "wav"}).encode()
    headers = {"Content-Type": "application/json"}
    if "openai.com" in url:
        key = os.environ.get("OPENAI_API_KEY", "")
        if key and not key.startswith("${"):
            headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url + "/audio/speech", data=body, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, round(time.time() - t0, 3), len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, round(time.time() - t0, 3), 0
    except Exception as e:
        return f"ERR({type(e).__name__})", round(time.time() - t0, 3), 0


def main():
    args = [a for a in sys.argv[1:]]
    down = set()
    if "--down" in args:
        i = args.index("--down")
        down = {x.strip().lower() for x in args[i + 1].split(",")}
        del args[i:i + 2]
    voices = [(a, "") for a in args] if args else DEFAULT_VOICES

    urls = configured_tts_urls()
    print(f"Configured TTS chain: {' -> '.join(engine_name(u) for u in urls)}")
    if down:
        print(f"Simulating OFFLINE: {', '.join(sorted(down))}")
    print()

    for voice, hint in voices:
        print(f"voice '{voice}'" + (f"  (a {hint} voice)" if hint else ""))
        served = None
        for url in urls:
            eng = engine_name(url)
            if any(ENGINE_PORT.get(d, d) in url for d in down):
                print(f"   {eng:7} {url:34}  [skipped — simulated offline]")
                continue
            code, dt, nbytes = try_endpoint(url, voice)
            ok = code == 200
            tag = "SERVED ✓" if ok else ("reject→next" if code in (400, 404) else f"fail→next")
            print(f"   {eng:7} {url:34}  http={code} {dt}s {tag}")
            if ok:
                served = eng
                break
        print(f"   => {'served by ' + served if served else 'ALL ENDPOINTS FAILED'}\n")


if __name__ == "__main__":
    main()
