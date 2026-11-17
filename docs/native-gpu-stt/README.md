# Native GPU speech recognition (faster-whisper)

> **TL;DR:** With an NVIDIA GPU, speech recognition can run natively: `faster-whisper-server.py`
> serves the OpenAI transcription API on port 2022 from its own venv, on the GPU, with no
> Docker and no sudo. It replaces whisper-proxy + the Docker whisper when
> `STT_ENGINE=faster-whisper`. The installer recommends it when `nvidia-smi` lists a GPU.
> Switch back with `STT_ENGINE=docker`.

| Field        | Value |
|--------------|-------|
| Status       | Built, wired into `voicemode-switch` and `install.sh`; not yet enabled on the dev machine |
| Created      | 2026-11-16 |
| Last Updated | 2026-11-17 (CPU fallback) |
| Project      | voicemode-local |

## Why

Speech recognition was the slow part of a voice turn. Measured 2026-11-16 on the dev machine
(RTX 4080 Laptop, WSL2), one 7.6 s spoken clip, n=5 runs each:

| Engine | Median | Range |
|---|---|---|
| Docker openai-whisper-asr `base`, CPU | 11.2 s | 5.4-21.1 s |
| faster-whisper `base`, GPU, float16 | 1.4 s | 1.2-1.5 s (first run 5.6 s) |
| faster-whisper `small`, GPU, float16 | 2.6 s | 1.9-3.0 s |

Model load: base 3.6 s; small 110 s the first time (that includes the download).

Measured again 2026-11-16 through the start wrapper (`VML_GPU_TESTS=1` test, model small,
already cached): ready (loaded + warm-up) in 11.7 s; a 2.8 s spoken clip (Kokoro af_sky,
24 kHz WAV) took 0.55 / 1.08 / 0.81 s over three requests (n=3), transcript exact.

Disk (measured 2026-11-16, `du`): the venv is 2.6 GB, of which 2.1 GB are the nvidia pip
packages; the `small` model is 464 MB in the Hugging Face cache, `base` 142 MB.

## What Was Done

- `faster-whisper-server.py` (repo root): stdlib `ThreadingHTTPServer` on 127.0.0.1.
  - `POST /v1/audio/transcriptions`: multipart `file`, `model` (ignored), `language`
    (`auto` or empty = detect), `prompt` (passed as `initial_prompt`), `response_format`
    `json` (default) | `text` | `verbose_json`. voice-mode sends `text` and `language=auto`.
  - `GET /health`: 200 only once the model is loaded **and** a warm-up transcription of 1 s of
    silence succeeded; 503 before, and after a failed load (with the error). The old
    whisper-proxy answers 200 even when its backend is dead; this one does not.
    `GET /v1/models` is 503 until ready too. Transcription requests before ready get 503.
  - One transcription at a time (a lock around the model; the segment generator is consumed
    inside it, since faster-whisper does the work lazily). Requests that arrive meanwhile wait.
  - One flushed log line per request: audio seconds and transcription seconds.
  - Args: `--host` (127.0.0.1), `--port` (2022), `--model` (small), `--device` cuda|cpu|auto (auto),
    `--compute-type` (float16 on cuda, int8 on cpu), `--model-dir` (default: HF cache).
  - If loading fails the process exits 1, so its PID file shows it dead.
- `scripts/faster-whisper-start.sh <venv> [server args]`: sets `LD_LIBRARY_PATH` from the venv's
  nvidia packages, then execs the server with the venv's python. `voicemode-switch` and
  `install.sh` both start it this way.
- `voicemode-switch`: config key `STT_ENGINE` in `~/.voicemode-local/config`.
  `faster-whisper`: `ensure` and `start` start the server on 2022 via the wrapper (venv from
  `FASTER_WHISPER_VENV`, default `~/.voicemode-local/faster-whisper-venv`; model from
  `FASTER_WHISPER_MODEL`, default `small`; bind from `FASTER_WHISPER_HOST`, default 0.0.0.0)
  instead of whisper-proxy; `stop` and `status` handle
  `/tmp/faster-whisper-server.pid`. Unset, `docker` or `whisper-cpp`: exactly the behaviour from
  before (`whisper-cpp` is still selected by `WHISPER_CPP_MODEL`).
- `install.sh` + `lib/stt.sh`: when `nvidia-smi -L` lists a GPU (Linux/WSL), asks with native
  GPU as the default; creates the venv (`uv venv` + `uv pip install faster-whisper
  nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"`) and writes `STT_ENGINE=faster-whisper`,
  `FASTER_WHISPER_VENV`, `FASTER_WHISPER_MODEL=small`. Without a GPU nothing changes.
  `--stt=docker` (or `VOICEMODE_STT=docker`) forces the Docker path; `--stt=native-gpu` forces
  native. A failed venv install falls back to Docker and rewrites `STT_ENGINE=docker`.

`voicemode-switch start` with `STT_ENGINE` other than `docker` brings up only the containers
in use (Kokoro, plus Piper when `PIPER_ENABLED=true`), says it is not starting the Docker
whisper, and starts the native engine (faster-whisper, or whisper.cpp for `whisper-cpp`).
With `STT_ENGINE` unset or `docker`, `start` is unchanged.

## Gotchas

1. **The CUDA libraries must be on `LD_LIBRARY_PATH` before the process starts.** ctranslate2
   dlopens cuBLAS and cuDNN at import; setting the variable in-process is too late. The pip
   packages are namespace packages, so the lib dirs are `nvidia.cublas.__path__[0] + "/lib"` and
   `nvidia.cudnn.__path__[0] + "/lib"`. Hence the wrapper. (Found in the 2026-11-16 measurement
   session; the wrapper path itself is what the GPU test exercises.)
2. **PyAV 19 breaks `faster_whisper.decode_audio`** (`open() got an unexpected keyword argument
   'metadata_errors'`), and `av<15` does not build here. So the server decodes WAV itself with
   the stdlib `wave` module + numpy (8/16/24/32-bit PCM, any rate, any channel count, mixed to
   mono and resampled to 16 kHz float32) and hands the array to `model.transcribe`.
   voice-mode sends WAV when the **first** STT endpoint is local; with a remote endpoint first
   (the `hybrid` mode: OpenAI, then local) it compresses to mp3. So anything that is not WAV
   (and a WAV the `wave` module cannot read, e.g. float samples) is piped through an `ffmpeg`
   binary on PATH: `ffmpeg -i pipe:0 -f s16le -ac 1 -ar 16000 pipe:1`, no shell, 30 s timeout.
   ffmpeg failing or timing out gets a **422** with ffmpeg's last error line. With no ffmpeg on
   PATH such uploads get a **415** naming the format (mp3, ogg, flac, m4a, webm, or
   unrecognised).
3. **Bind address.** The server's own default is 127.0.0.1 (`--host`). `voicemode-switch` and
   `install.sh` pass `FASTER_WHISPER_HOST` from the config, default **0.0.0.0**: that is what
   whisper-proxy.py binds (it is started without `--host`), so replacing the proxy keeps every
   reachability it had, e.g. Windows reaching WSL's 2022 through localhost forwarding. Set
   `FASTER_WHISPER_HOST=127.0.0.1` to keep it inside WSL. A test ties the switch default to the
   proxy's default.
4. `ensure` skips any port that already answers. A running whisper-proxy on 2022 therefore keeps
   the port: stop it before switching (see below).

## How to switch

To native (after a venv exists, e.g. from `install.sh`, or by hand:
`uv venv ~/.voicemode-local/faster-whisper-venv && uv pip install --python
~/.voicemode-local/faster-whisper-venv/bin/python faster-whisper nvidia-cublas-cu12
"nvidia-cudnn-cu12==9.*"`):

```bash
# ~/.voicemode-local/config
STT_ENGINE=faster-whisper
FASTER_WHISPER_VENV=/home/<you>/.voicemode-local/faster-whisper-venv
FASTER_WHISPER_MODEL=small

kill "$(cat /tmp/whisper-proxy.pid)"      # free port 2022
voicemode-switch ensure
curl http://127.0.0.1:2022/health          # 503 while loading, then 200
```

Back to Docker:

```bash
# ~/.voicemode-local/config
STT_ENGINE=docker

voicemode-switch stop                      # or: kill "$(cat /tmp/faster-whisper-server.pid)"
voicemode-switch start                     # or ensure
```

Routing in `~/.voicemode/voicemode.env` does not change: both engines serve
`http://127.0.0.1:2022/v1`. No Claude Code restart is needed.

## Who holds port 2022 (identity check)

Why: on 2026-11-17 a WSL restart killed the native server; the next shell's `~/.bashrc`
autostart ran `whisper-proxy.py` on the free 2022, and `ensure` then saw "2022 answers" and never
started the configured engine. The proxy's `/health` also answered 200 while its Docker backend
was dead (a real transcription returned 502), so speech input ran on the old path unnoticed for
about 7 hours. Three guards, all tested:

- `whisper-proxy.py` `/health` reports the backend (1.5 s probe, cached 2 s): 200
  `{"status":"ok","engine":"whisper-proxy","backend":"ok"}`, else 503 `backend_unreachable`.
- `whisper-proxy.py` exits 0 at startup when `STT_ENGINE=faster-whisper` (config path from
  `VOICEMODE_LOCAL_CONFIG` for tests, else `~/.voicemode-local/config`); `--force` overrides.
  The bashrc autostart can therefore no longer take 2022 from the engine.
- `voicemode-switch ensure`: with `STT_ENGINE=faster-whisper`, 2022 counts as healthy only if
  `GET /health` has `"engine": "faster-whisper"`. Another holder is named in one line
  (`2022 is held by whisper-proxy (PID n), not the configured faster-whisper engine`). It is
  stopped, by exact pid, only if `/tmp/whisper-proxy.pid` or the command line
  (`python3 .../whisper-proxy.py --port 2022`) proves it is voicemode's own; the pid file wins
  when it matches. Without a pid file, only processes of the current user whose command line
  contains THIS install's script path (`<install dir>/whisper-proxy.py`, regex-escaped) match, so
  a lookalike from another checkout or a test is never stopped. If the holder cannot be proven ours, nothing is killed, the line says so and
  `ensure` exits non-zero. The same rule covers native GPU Kokoro: with `KOKORO_ONNX_GPU=true`,
  8880 is healthy only if `/health` says provider `CUDAExecutionProvider` (our own server that
  reports no provider yet is still loading and is left alone). Tests:
  `tests/test_switch_port_identity.py`, `tests/test_whisper_proxy.py`.

## CPU fallback

**Why.** On 2026-11-17 a WSL restart killed the GPU engine on 2022 and speech input was gone
until a session ran `ensure` again. A second faster-whisper on the CPU fails independently of
CUDA, so with it routed after 2022, voice-mode moves on to it when 2022 refuses or errors.

**Config** (`~/.voicemode-local/config`; all default off, so an existing setup is unchanged):

| Key | Default | Meaning |
|---|---|---|
| `STT_CPU_FALLBACK` | false | `true` runs the CPU twin (only with `STT_ENGINE=faster-whisper`) |
| `STT_CPU_FALLBACK_PORT` | 2023 | its port |
| `STT_CPU_FALLBACK_MODEL` | base | its model |

`install.sh` writes `STT_CPU_FALLBACK=true` when it sets up the native GPU engine; opt out with
`--no-cpu-fallback` or `VOICEMODE_CPU_FALLBACK=false` (then it writes `false`).

**What runs.** `ensure` and `start` run `scripts/faster-whisper-start.sh <FASTER_WHISPER_VENV>
--host <FASTER_WHISPER_HOST> --port 2023 --model base --device cpu --compute-type int8`, detached
(`setsid`), pid file `/tmp/faster-whisper-cpu.pid`, log `/tmp/faster-whisper-cpu.log`. Same
venv as the GPU engine: no second install. `ensure` starts it whenever 2022 is among the
configured local ports, even before the routing names 2023. The port counts as healthy only if
`/health` says `"engine": "faster-whisper"` and `"device": "cpu"`; our own server still loading
(503, `"status": "loading"`, `"device": "cpu"`) is left alone. Any other holder is stopped only
when its pid file or command line proves it is this install's own (`<install
dir>/faster-whisper-server.py ... --port 2023`, exact pid); otherwise `ensure` names it in one
line, kills nothing and exits non-zero. `stop` stops it the same way (a stale pid file whose pid
now belongs to something else is removed, not killed); `status` shows it with its device and
model; `health` lists it.

**Routing.** `voicemode-switch local|localonly|hybrid|piper` put `http://127.0.0.1:2023/v1` right
after 2022 in `VOICEMODE_STT_BASE_URLS` (local: `2022, 2023, OpenAI`). With the fallback off the
list is exactly as before. Takes effect after the voice-mode reconnect (`/mcp`), like any routing
change.

**Cost and model choice.** Measured 2026-11-17 on this machine (14 cores), the 7.6 s clip
`~/.cache/voicemode-local/stt-bench/speech.wav`, the server on a spare port through the wrapper,
`curl -F file=@speech.wav .../v1/audio/transcriptions`, n=3 per model, every transcript exact:

| Model (CPU int8) | load ~5 | load ~25 | idle RSS | disk |
|---|---|---|---|---|
| base | 2.5 / 3.9 / 5.4 s | 23.3 / 27.3 / 29.8 s | 256 MiB (268 MiB after use) | 142 MB |
| small | 14.3 / 16.7 / 16.9 s | 28.0 / 41.3 / 47.2 s | 716 MiB (728 MiB after use) | 464 MB |

("load" is the 1-minute load average at each run; during the quiet small runs it rose from 8 to
13.) Only base stays within 1.5x the clip length (11.4 s), and only on a quiet machine: under load
~25 both are 3-6x slower than the clip. The fallback is a degraded mode, not a replacement.
Model load to ready: base 7 s quiet / 111 s loaded, small 16 s / 48 s.

**What a failover looks like.** With 2022 down (connection refused) voice-mode tries 2023 at
once; the turn is slower (seconds on a quiet machine, tens of seconds under load) and
`/tmp/faster-whisper-cpu.log` gets the request line (`#N audio=... transcribe=...`). If 2022
is up but hangs, voice-mode waits for its STT timeout first (`VOICEMODE_STT_TIMEOUT_LOCAL`,
see `docs/stt-61s-stall`). Not observed end to end in a live voice session yet.

**Switch it off:** `STT_CPU_FALLBACK=false` in the config, `voicemode-switch stop` (or
`kill "$(cat /tmp/faster-whisper-cpu.pid)"`), and re-run your mode (`voicemode-switch local`) to
take 2023 out of the routing.

## Verification

- `voicemode-switch status` shows "faster-whisper server running (PID ...)" and the 2022 health.
- `/tmp/faster-whisper-server.log` has `model small on cuda ready in ...` and one line per
  request: `#N audio=7.60s transcribe=...s`.
- Tests: `tests/test_faster_whisper_server.py` (fake model), `tests/test_faster_whisper_start.py`,
  `tests/test_switch_stt_engine.py`, `tests/test_install_stt.py`; the CPU fallback:
  `tests/test_switch_cpu_fallback.py`, `tests/test_switch_fallback_routing.py`. The real GPU path:
  `VML_GPU_TESTS=1 pytest tests/test_faster_whisper_server.py -k real_gpu -s` (venv from
  `VML_GPU_VENV`, default `~/.cache/voicemode-local/stt-gpu-venv`; model from `VML_GPU_MODEL`,
  default small; speech generated in-process with kokoro-onnx).
