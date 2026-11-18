# Native GPU speech output (kokoro-onnx on CUDA)

> **TL;DR:** With an NVIDIA GPU, Kokoro can run natively on the GPU: the same
> `kokoro-onnx-server.py` from its own venv with `onnxruntime-gpu==1.23.2`, started through
> `scripts/kokoro-gpu-start.sh` with `--require-gpu`, on port 8880 **instead of** the Docker
> Kokoro. No Docker, no sudo. Config: `KOKORO_ONNX=true` + `KOKORO_ONNX_GPU=true`. The
> installer recommends it when `nvidia-smi` lists a GPU. Two pip traps make it run on the CPU
> without saying so; `--require-gpu` turns that into an exit 1 with the cause.

| Field        | Value |
|--------------|-------|
| Status       | Built, wired into `voicemode-switch` and `install.sh`; not yet enabled on the dev machine |
| Created      | 2026-11-17 |
| Last Updated | 2026-11-17 |
| Project      | voicemode-local |

## Why

Measured 2026-11-17 by a measuring session (not re-derived here; full method and raw
numbers in `~/.cache/voicemode-local/measure/RESULTS.md` on the dev machine), RTX 4080
Laptop, WSL2, voice af_sky, time until the full audio is back, median of n=5:

| Engine | short (55 chars) | medium (270) | long (980) |
|---|---|---|---|
| Docker Kokoro-FastAPI, GPU (8880) | 0.75 s | 6.5 s | 15.9 s |
| kokoro-onnx, CPU (8882) | 13.9 s | 63.7 s | 157.5 s |
| kokoro-onnx, CUDA (this setup, unmodified server) | 1.35 s | 4.6 s | 19.4 s |

**Caveat:** the machine was shared and very busy (1-minute load 23-53 on 12 cores during the
runs), so absolute numbers are inflated, the CPU ones most; treat the ratios as indicative.
On those numbers native GPU is about as fast as the Docker GPU Kokoro and 10-20x faster than
kokoro-onnx on the CPU, without the multi-GB Kokoro-FastAPI GPU image or Docker.

Validated in this work (2026-11-17, load 15-20):

- The real server on CUDA through the wrapper with `--require-gpu`
  (`VML_GPU_TESTS=1` test below): started, `/health` reported `CUDAExecutionProvider`, and
  synthesised "Hello from the GPU." (82,988 bytes of WAV). First request after start: 8.34 s
  and 2.96 s (n=2 server starts; the first request includes CUDA warm-up, so these are not
  comparable with the medians above).
- The same venv started **without** the wrapper (no CUDA libraries on the path, no
  `ONNX_PROVIDER`) with `--require-gpu`: onnxruntime tried TensorRT/CUDA, printed
  "Falling back to ['CPUExecutionProvider']", and the server exited 1 with the cause (n=1).
- Disk (`du -sh`, n=1): the venv is 3.3 GB (2.7 GB of it the nvidia pip packages,
  463 MB onnxruntime). The English model files (`models/kokoro`, 338 MB) are shared with the
  CPU server.

## The traps (why `--require-gpu` exists)

1. **The default pip `onnxruntime-gpu` needs CUDA 13.** 1.32.0 (current on 2026-11-17)
   lists `CUDAExecutionProvider`, fails to load it with the CUDA 12 pip libraries
   (`libcublasLt.so.13 not found`) and silently falls back to the CPU (found by the
   measuring session). Pin **`onnxruntime-gpu==1.23.2`** (CUDA 12 build).
2. **kokoro-onnx requires the CPU `onnxruntime`,** which installs into the same `onnxruntime/`
   module directory. Installing `kokoro-onnx ... onnxruntime-gpu==1.23.2` in one `uv pip
   install` left `onnxruntime` 1.32.0 (CPU) in charge: providers
   `['AzureExecutionProvider', 'CPUExecutionProvider']` (validated 2026-11-17, one venv,
   twice). Fix: `uv pip uninstall onnxruntime` (that breaks the shared directory), then
   `uv pip install --reinstall-package onnxruntime-gpu onnxruntime-gpu==1.23.2`; providers
   then `['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']`.
   `vm_setup_kokoro_gpu` does exactly that and then checks that CUDA is offered.
3. **The CUDA libraries must be on `LD_LIBRARY_PATH` before Python starts.** They come from
   `nvidia-*-cu12` pip packages, which are namespace packages: each lib dir is
   `nvidia.<pkg>.__path__[0] + "/lib"`. And kokoro_onnx takes the provider from env
   `ONNX_PROVIDER` (the server passes none). Hence the wrapper.

Any of the three ends in the same place: a server that answers, on the CPU, 10-20x slower.
With `--require-gpu` (or `VOICEMODE_KOKORO_REQUIRE_GPU=1`) the server checks the provider
list of the session it really got (`kokoro_onnx.Kokoro.sess.get_providers()`) after loading
the model, and exits 1 unless `CUDAExecutionProvider` is first, with: "onnxruntime-gpu loaded
no CUDA provider; the default pip build needs CUDA 13: install onnxruntime-gpu==1.23.2",
plus what the session runs on and the onnxruntime version and providers. If that version is
already 1.23.2, the cause is trap 3 (started without the wrapper).

## What Was Done

- `kokoro-onnx-server.py`: `--require-gpu` / `VOICEMODE_KOKORO_REQUIRE_GPU=1` as above (the
  German model, when loaded, is checked too). `/health` and `/v1/models` report the active
  provider (`"provider": "CUDAExecutionProvider"`, `"CPUExecutionProvider"`, or `null` before
  the model is loaded) — this field is there with or without the flag; nothing else changes
  without it.
- `scripts/kokoro-gpu-start.sh <venv> [server args]`: every `nvidia/*/lib` dir of the venv on
  `LD_LIBRARY_PATH`, `ONNX_PROVIDER=CUDAExecutionProvider`, then execs the server with the
  venv's python. `scripts/faster-whisper-start.sh` is unchanged.
- `voicemode-switch` (`~/.voicemode-local/config`): with `KOKORO_ONNX=true` and
  `KOKORO_ONNX_GPU=true`, `ensure` and `start` run the server through the wrapper with
  `--require-gpu` on `KOKORO_ONNX_PORT` (venv `KOKORO_ONNX_VENV`, default
  `~/.voicemode-local/kokoro-gpu-venv`), same PID file and log as the CPU server
  (`/tmp/kokoro-onnx-server.pid`, `/tmp/kokoro-onnx-server.log`). On 8880, `start` does not
  start the Docker kokoro and says so (stopping it first if it holds 8880); it starts the
  Docker whisper only when `STT_ENGINE=docker`, Piper when `PIPER_ENABLED=true`, and says
  "No Docker containers needed" otherwise. `status` shows the provider from `/health`.
  `KOKORO_ONNX_GPU` without `KOKORO_ONNX=true` is ignored. Unset: commands and output as
  before (checked by running main's and this branch's script side by side in the test
  harness for 5 configs x ensure/status/start/stop).
- `install.sh` + `lib/tts.sh`: when `nvidia-smi -L` lists a GPU (Linux/WSL), asks with native
  GPU as the default; creates the venv and writes `KOKORO_ONNX=true`, `KOKORO_ONNX_GPU=true`,
  `KOKORO_ONNX_VENV`, `KOKORO_ONNX_PORT=8880`; in Docker install mode it does not start the
  Docker kokoro. `--tts=docker` (or `VOICEMODE_TTS=docker`) keeps the Docker Kokoro;
  `--tts=native-gpu` forces native. A failed venv install falls back to the Docker Kokoro
  (Docker install mode: `KOKORO_ONNX=false`, `docker compose up -d kokoro`) or to Kokoro ONNX
  on the CPU (native install mode, which has no Docker). The German model download step is
  unchanged.

## Gotchas

- `ensure` skips any port that already answers. A running Docker Kokoro on 8880 therefore
  keeps the port: stop it first (`docker compose stop kokoro`), or use `voicemode-switch
  start`, which does.
- Native GPU on a port other than 8880 (e.g. `KOKORO_ONNX_PORT=8882`) runs alongside the
  Docker Kokoro, which `start` then still starts.
- The vml_supervisor / FELIX profile (`vml_profiles/claude.py`) starts its own
  `kokoro-onnx-server.py` and knows nothing of this setting.

## How to switch

To native GPU (after a venv exists, e.g. from `install.sh`, or by hand with
`bash -c '. lib/tts.sh && vm_setup_kokoro_gpu ~/.voicemode-local/kokoro-gpu-venv'` from the
checkout):

```bash
# ~/.voicemode-local/config
KOKORO_ONNX=true
KOKORO_ONNX_GPU=true
KOKORO_ONNX_PORT=8880
KOKORO_ONNX_VENV=/home/<you>/.voicemode-local/kokoro-gpu-venv

docker compose stop kokoro                 # free port 8880 (or: voicemode-switch start)
voicemode-switch ensure
curl http://127.0.0.1:8880/health          # "provider": "CUDAExecutionProvider"
```

Back to the Docker Kokoro:

```bash
# ~/.voicemode-local/config
KOKORO_ONNX=false
KOKORO_ONNX_GPU=false

kill "$(cat /tmp/kokoro-onnx-server.pid)"
voicemode-switch start                     # or ensure
```

Routing in `~/.voicemode/voicemode.env` does not change: both serve
`http://127.0.0.1:8880/v1`. No Claude Code restart is needed.

## CPU fallback

**Why.** On 2026-11-17 the GPU Kokoro on 8880 was killed (by a test) and, with the Docker
Kokoro gone, nothing local spoke English until a session ran `ensure`. A Kokoro on the CPU
fails independently of CUDA, so routed after 8880 it keeps speech output local.

**Config** (`~/.voicemode-local/config`; default off, so an existing setup is unchanged):

| Key | Default | Meaning |
|---|---|---|
| `KOKORO_CPU_FALLBACK` | false | `true` runs the CPU twin (only with `KOKORO_ONNX=true` + `KOKORO_ONNX_GPU=true`) |
| `KOKORO_CPU_FALLBACK_PORT` | 8882 | its port; must differ from `KOKORO_ONNX_PORT` (if equal it is not started, and `ensure`/`start` say so) |

**Opt-in** (Robert, 2026-11-17). When `install.sh` sets up the native GPU Kokoro it asks, default
No, with the measured costs (2026-11-17, n=3, this machine, see "Cost" below):

> It keeps English speech output working if the GPU server dies or CUDA fails. Cost: no extra
> disk (same models and GPU venv), about 450 MiB RAM idle and up to about 750 MiB in use. It is
> slower than real time under load.

It writes `KOKORO_CPU_FALLBACK=true` only on a yes, `--cpu-fallback`, or
`VOICEMODE_CPU_FALLBACK=true`; otherwise `false`. A run without input answers No.
`--no-cpu-fallback` / `VOICEMODE_CPU_FALLBACK=false` keep it off without asking.

**What runs.** `ensure` and `start` run `scripts/kokoro-cpu-start.sh <KOKORO_ONNX_VENV> --port
8882 --model-dir <install>/models/kokoro --no-download`, detached (`setsid`), pid file
`/tmp/kokoro-cpu-fallback.pid`, log `/tmp/kokoro-cpu-fallback.log`. The wrapper runs the server
from the **same GPU venv** (onnxruntime-gpu also has `CPUExecutionProvider`; no second venv),
sets `ONNX_PROVIDER=CPUExecutionProvider`, puts no CUDA libraries on the path, drops
`--require-gpu` and clears `VOICEMODE_KOKORO_REQUIRE_GPU`; without that venv it uses the repo's
`.venv` python. The bind is the server default (0.0.0.0), as for the GPU server. `ensure`
starts it whenever the GPU Kokoro's port is among the configured local ports. The port counts
as healthy only if `/health` says `"provider": "CPUExecutionProvider"`; any other holder is
stopped only when its pid file or command line proves it is this install's own
(`<install dir>/kokoro-onnx-server.py ... --port 8882`, exact pid), otherwise `ensure` names it
in one line, kills nothing and exits non-zero. `stop` stops it the same way (a stale pid
file's unrelated pid is never killed); `status` shows it with its provider; `health` lists it.

**Routing.** `voicemode-switch local|localonly|hybrid|piper` put `http://127.0.0.1:8882/v1`
after the Kokoro and Piper URLs, before OpenAI (local: `8880, 8881, 8882, OpenAI`; piper:
`8881, 8880, 8882, OpenAI`). They also write `VOICEMODE_TTS_TIMEOUT_LOCAL=90` into the managed
block, **unless** `voicemode.env` already sets it (a value you set is never overwritten, and an
`export`ed one counts too); the default 30 s cuts a CPU Kokoro off on longer parts (measured
earlier: 3-4.5x slower than real time on a loaded machine). Turning the fallback off and
re-running the mode removes both lines. With the fallback off the lists are exactly as before.
A user value set in the real environment instead of the file is not seen by the switch.

**Cost.** Measured 2026-11-17 on this machine: `kokoro-cpu-start.sh` with the real
`~/.voicemode-local/kokoro-gpu-venv` on a spare port, passed `--require-gpu` on purpose: it ran,
`/health` said `CPUExecutionProvider`, the process env had `ONNX_PROVIDER=CPUExecutionProvider`;
ready in 3.1 s; idle RSS 453 MiB, 746 MiB after three requests; a 108-character sentence (6.2 s
of audio, af_sky) took 4.91 / 4.90 / 4.80 s (n=3) at load 3.4-4.3 on 14 cores. That is faster
than real time on a quiet machine; under load expect the 3-4.5x slower-than-real-time figure
above. No extra disk: the venv and the model files are the GPU server's.

**What a failover looks like.** With 8880 down (connection refused, or an error) voice-mode
moves to the next URL: Piper, which rejects Kokoro voices fast, then 8882. English speech keeps
the same voice, slower, and `/tmp/kokoro-cpu-fallback.log` shows the requests. A long part may
still exceed 90 s on a loaded machine; `VOICEMODE_TTS_CHUNK_CHARS` (default 300) keeps parts
short. Not observed end to end in a live voice session yet.

**Switch it off:** `KOKORO_CPU_FALLBACK=false` in the config, `voicemode-switch stop` (or
`kill "$(cat /tmp/kokoro-cpu-fallback.pid)"`), and re-run your mode (`voicemode-switch local`).

## Provider and threads

Robert, 2026-11-17: "include as options, GPU as default as long as a supported GPU is
available", and "users should be able to decide by install and configuration". Before this
the server set no provider and no session options: the provider came from kokoro-onnx's
`ONNX_PROVIDER`, the threads from onnxruntime's default.

| Setting | Server | Meaning |
|---|---|---|
| `KOKORO_PROVIDER=auto` | `--provider auto` / `VOICEMODE_KOKORO_PROVIDER=auto` | CUDA when onnxruntime offers it **and** the session really runs on it; else the CPU, with one log line saying why and `/health` `fallback_reason`. Never picks DirectML or CoreML. |
| `KOKORO_PROVIDER=cuda` | `--provider cuda` | CUDA or exit 1 naming the cause (the `--require-gpu` rule). |
| `KOKORO_PROVIDER=cpu` | `--provider cpu` | CPUExecutionProvider only. |
| `KOKORO_PROVIDER=directml` / `coreml` | `--provider directml` / `coreml` | DmlExecutionProvider / CoreMLExecutionProvider, or exit 1. Accepted, never offered by the installer. |
| unset | nothing passed | exactly as before (`kokoro_onnx.Kokoro(model, voices)`; GPU engine: `--require-gpu`). |
| `KOKORO_THREADS=N` | `--threads N` / `VOICEMODE_KOKORO_THREADS` | onnxruntime intra-op threads; 0 or unset = its default. Also passed to the CPU fallback. |

The flag wins over the environment. With the GPU engine a `KOKORO_PROVIDER` **replaces**
`--require-gpu`; `--require-gpu` with a provider other than cuda is refused (exit 2). The CPU
fallback (`scripts/kokoro-cpu-start.sh`) drops any provider: it is always the CPU.

**How it is applied.** With a provider or threads the server builds the onnxruntime session
itself (`SessionOptions.intra_op_num_threads`, the provider list) and passes it to
`kokoro_onnx.Kokoro.from_session`, which exists in kokoro-onnx 0.6.1 (ours) and 0.4.9
(FELIX; read from the 0.4.9 wheel on PyPI, 2026-11-17). Nothing in onnxruntime is patched.
With only threads set, the provider list is what kokoro-onnx would pick (`ONNX_PROVIDER`, else
its `resolve_providers`, else the CPU).

**Logs and status.** The first log line is `Kokoro ONNX options: provider requested auto,
threads 4`; after loading, `Kokoro ONNX provider: CUDAExecutionProvider (requested: auto),
threads: 4` (with `reason: ...` when auto fell back). With neither option set the log is as
before. `/health` adds `provider_requested`, `fallback_reason` (auto) and `threads` when set.
`voicemode-switch status` shows the settings and how to change them.

**Installer.** With the GPU engine `install.sh` asks auto / cpu / cuda (default auto, also with
no input) and writes `KOKORO_PROVIDER` and `KOKORO_THREADS=0` (an existing value is kept) to
`~/.voicemode-local/config`. `--kokoro-provider <name>` or `VOICEMODE_KOKORO_PROVIDER`
decides without asking. Without the GPU engine nothing is asked or written unless forced. The
threads are never asked. Change later: edit the config, then `voicemode-switch stop &&
voicemode-switch start`.

What has been run:

| Path | Run? |
|---|---|
| CUDA on Linux/WSL (RTX 4080 Laptop, onnxruntime-gpu 1.23.2), `--provider auto --threads 4` through the GPU wrapper | yes, 2026-11-17, n=1: `/health` CUDAExecutionProvider, a sentence spoken |
| CPU, `--provider cpu --threads N`, repo `.venv` (onnxruntime 1.29.0) | yes, 2026-11-17 (timings below) |
| CUDA on Windows | not run |
| DirectML (Windows) | not run |
| CoreML (macOS) | not run |
| kokoro-onnx 0.4.9 (FELIX) with `from_session` | read in the source only, not run |

**CPU threads, measured 2026-11-17/18 (n=3 each, warm, one English sentence of 7.06 s audio,
voice af_sky, repo `.venv`: kokoro-onnx 0.6.1, onnxruntime 1.29.0, 14 cores, spare port).**
There was no quiet window: the 1-minute load was 22-55 throughout (other sessions). Seconds per
request, median (range), load during the three requests:

| Setting | Run 1 (order default to 8) | Run 2 (reverse order) |
|---|---|---|
| unset (constructor, as before) | 46.4 (40.2-51.4), load 36-43 | 61.9 (61.4-73.8), load 49-55 |
| `--provider cpu`, threads default | 16.0 (12.5-21.1), load 34-37 | 51.7 (42.1-68.8), load 49-55 |
| `--threads 2` | 13.7 (13.0-14.0), load 23-31 | 48.8 (26.3-54.5), load 45-50 |
| `--threads 4` | 13.9 (13.7-23.5), load 22-24 | 46.2 (39.9-55.3), load 40-47 |
| `--threads 8` | 28.8 (19.1-29.3), load 30-44 | 30.8 (27.7-33.0), load 42-45 |

Read with care. The first two rows run the same session settings (CPU, onnxruntime's default
threads) and still differ 3x in run 1, which shows how large the load noise is. Under this load no
thread count was faster in both runs. Defaults were not changed. The question (FELIX's 47 threads
on a P/E-core CPU) needs a quiet machine and the Windows laptop.

## Piper on the GPU (measured, not built)

2026-11-18, n=3 each, warm model, in-process `PiperVoice.synthesize_wav` (the call
`piper-proxy.py` makes), `de_DE-thorsten-high` (p_de_thorsten), the 7.5 s German sentence
from the brief, piper-tts 1.8.0, throwaway venv with onnxruntime-gpu 1.23.2 and the nvidia pip
libraries on LD_LIBRARY_PATH (the `scripts/kokoro-gpu-start.sh` recipe), order CPU, CUDA, CPU:

| Run | Session providers (read from the session) | Seconds, median (range) | Audio | Load |
|---|---|---|---|---|
| CPU 1 | CPUExecutionProvider | 62.4 (61.4-74.0) | 7.33-7.64 s | 62-79 |
| CUDA (`PiperVoice.load(..., use_cuda=True)`) | CUDAExecutionProvider, CPUExecutionProvider | 2.60 (2.36-3.50) | 7.23-7.64 s | 67 |
| CPU 2 | CPUExecutionProvider | 46.8 (37.9-50.1) | 7.52-7.69 s | 55-60 |

Model load: CPU 13.9 s and 16.2 s, CUDA 43.9 s (n=1 each, same load). Under this load CUDA is
about 18-24x faster than the CPU and faster than real time. The CPU numbers are far above the
quiet-machine baseline the measuring session was given (9.5 s and 12.7 s at load 16), so the CPU
side is load-bound; no quiet-machine CPU run was possible here.

What a piper-proxy GPU option would take (not built):
- `load_piper_voice` passes `use_cuda=True` behind a setting (`PIPER_CUDA` / a `--cuda` flag),
  and the proxy preloads the model at start (44 s on CUDA here, before the first reply).
- Its own venv: piper-tts depends on the CPU `onnxruntime`, the same trap as kokoro-onnx, so
  `onnxruntime` is removed and `onnxruntime-gpu==1.23.2` reinstalled, then CUDA checked (3.3 GB
  measured for this venv).
- A start wrapper that puts the nvidia lib dirs on LD_LIBRARY_PATH (the Kokoro recipe).
- Fail loud or fall back visibly: read `voice.session.get_providers()` after loading; `/health`
  names the provider; the same auto / cuda / cpu choice as Kokoro.
- Installer and `voicemode-switch ensure/status` wiring, tests with a fake piper. GPU memory
  next to Kokoro and whisper was not measured.

## `start` and a holder of 8880 (CPU Kokoro ONNX)

`voicemode-switch start` with the CPU Kokoro ONNX on 8880 (`KOKORO_ONNX=true`, no GPU) used to
run `kill` on every pid `lsof -ti:8880` listed. Now: this install's own earlier
`kokoro-onnx-server.py --port 8880` is stopped by exact pid; a Docker container publishing 8880
whose image name contains `kokoro` is stopped with `docker stop <name>` (the name `docker ps
--filter publish=8880` reported); anything else gets one line naming it and `start` exits 1,
killing nothing. Behaviour change: a foreign holder of 8880 now stops `start` instead of being
killed; free the port yourself or set `KOKORO_ONNX_PORT`. Tests:
`tests/test_switch_start_8880.py`.

`install.sh` follows the same rules (`lib/ports.sh`, `vm_free_port`) where it used to run
`kill "$(lsof -ti:8880)"` (Step 7a) and `kill "$(lsof -ti:2022)"` (Step 7c, both STT
branches): its own earlier server or whisper-proxy (command line naming the install
directory and `--port`, current user) by exact pid, a Docker container publishing the port
whose name or image contains `kokoro` / `whisper` by `docker stop <name>`, anything else named
in one line and the install stops (exit 1) with nothing killed. Tests:
`tests/test_install_ports.py`.

## Warm-up (cold first request)

Measured 2026-11-18 on this WSL machine with spare-port servers, n=3 fresh starts each, the
same 2.4 s sentence, timed right after `/health` turned 200:

| engine | load before / after | before: req 1 / req 2 | after warm-up: req 1 / req 2 | warm-up at start |
|---|---|---|---|---|
| GPU Kokoro | 50-55 / 45-49 | 5.3-11.8 s / 0.9-1.2 s | 2.9-5.1 s / 1.0-1.35 s | 3.5-7.3 s |
| CPU Kokoro | 45-51 / 40-44 | 5.4-8.3 s / 5.8-12.0 s (no cold penalty) | 4.2-9.9 s / 3.7-11.0 s | 3.6-5.1 s |
| Piper (thorsten-high) | 41-46 / 38-42 | 13.4-19.4 s / 3.7-6.1 s | 6.2-8.6 s / 5.8-7.6 s | 6.8-8.1 s |

Where the cost was (in-process, load 26-44): GPU Kokoro's first `create()` 3.4-5.6 s, then
0.75-1.1 s (n=9); not the model load (done before the server listens), not espeak (first
phonemize 0.07-0.23 s, n=3), and not cuDNN algorithm search (`cudnn_conv_algo_search=HEURISTIC`
first call 4.3-5.6 s vs EXHAUSTIVE 3.4-4.4 s, n=3 each). After a "Hello." warm-up the first
real sentence still took 1.5-2.0 s vs 0.6-0.8 s warm (n=3): a smaller per-new-input cost the
warm-up does not remove.
Piper: not the synthesis (first and second alike) but `convert_wav`'s first call, 3.8-7.6 s,
its lazy scipy import.

So both servers run one tiny synthesis before they serve: Kokoro says "Hello." before it binds
its port, Piper says "Hallo." with the pre-loaded default voice (only that one) and converts
it. Log line `warm-up: <seconds> s`; a failing warm-up is logged and the server serves anyway.
Behaviour change: start takes the warm-up longer; old behaviour via `VOICEMODE_KOKORO_WARMUP=0`
/ `--no-warmup` and `VOICEMODE_PIPER_WARMUP=0` / `--no-warmup`. `/health` does not answer
during the warm-up (as during the model load), so nothing is reported healthy early and the
pid file keeps `ensure` from starting a second copy. Tests: `tests/test_kokoro_onnx_warmup.py`,
`tests/test_piper_proxy_warmup.py`.

Not covered: the first request after a long idle (39.8 s GPU, 38.0 s Piper, 73.1 s CPU on
2026-11-18, n=1 each) is NOT explained by this: a fresh start's cold request was 5-19 s.
Idle-dependent paging (or GPU clocks/power state) was not tested.

## Verification

- `voicemode-switch status`: "Kokoro ONNX (native GPU, port 8880)", the PID, and
  "Provider: CUDAExecutionProvider".
- `/tmp/kokoro-onnx-server.log`: "Kokoro ONNX provider: CUDAExecutionProvider", or the
  `--require-gpu` error and the server gone.
- Tests: `tests/test_kokoro_onnx_require_gpu.py` (fake kokoro_onnx), `tests/test_kokoro_gpu_start.py`,
  `tests/test_switch_kokoro_gpu.py`, `tests/test_install_tts.py`; the CPU fallback:
  `tests/test_kokoro_cpu_start.py`, `tests/test_switch_cpu_fallback.py`,
  `tests/test_switch_fallback_routing.py`; provider and threads:
  `tests/test_kokoro_onnx_provider.py` (fake onnxruntime), `tests/test_switch_kokoro_provider.py`.
  The real GPU path:
  `VML_GPU_TESTS=1 VML_KOKORO_GPU_VENV=<venv> pytest tests/test_kokoro_onnx_require_gpu.py -k real -s`
  (model dir from `VML_KOKORO_MODEL_DIR`, default `models/kokoro` in the checkout).

## Not known yet

- No idle-machine timing for any engine; no long-text or chunked-reply timing through this
  wrapper (only the measuring session's unmodified-server numbers above).
- The installer path has not been run end to end (only its functions with fakes, and the
  venv recipe by hand). Not tried on any GPU but the RTX 4080 Laptop, nor on native Linux.
- How much GPU memory it takes next to other GPU users was not measured.
