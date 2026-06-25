# GPU / CPU Compute Modes

> **TL;DR:** VoiceMode's Docker STT/TTS backends (Whisper + Kokoro) run on CPU, the
> NVIDIA GPU, or a **hybrid** (Kokoro GPU + Whisper CPU — the recommended mode with a
> GPU). Install auto-detects the GPU and defaults to hybrid; `voicemode-switch compute
> [gpu|hybrid|cpu]` swaps at any time. CPU mode caps Kokoro's cores so it can't saturate
> the machine and stall audio mid-sentence. The Whisper model is cached in a named volume
> so it isn't re-downloaded on every container recreate.

| Field        | Value |
|--------------|-------|
| Status       | Complete |
| Created      | 2026-06-25 |
| Last Updated | 2026-06-25 |
| Project      | voicemode-local |

## Why

Under several concurrent voice sessions, voice started dropping mid-sentence and
inter-response latency climbed. Root cause (measured): **CPU oversubscription**. On a
14-core box, load average sat at ~18. The dominant draws were `voicemode-kokoro`
running the **CPU-only** Kokoro image at ~550–670 % CPU (~6 cores) and a Next.js dev
server spiking on rebuilds. TTS streams audio in real time; when the Kokoro process
can't get CPU fast enough, the playback buffer underruns and the sentence cuts off.

Meanwhile the machine's RTX 4080 sat ~idle and **no voice service used it** — both the
Kokoro and Whisper containers had no GPU device request. Moving inference to the GPU
frees the cores the audio pipeline needs and makes each request faster.

## What Was Done

- **CPU cap on Kokoro** (`docker-compose.yml`): `deploy.resources.limits.cpus`
  (default 6, `KOKORO_CPUS`). Kokoro can no longer grab every core. Applied live to the
  running container with `docker update --cpus=6` (no restart) and persisted in compose.
- **GPU override** (`docker-compose.gpu.yml`): swaps Whisper → `:latest-gpu` and Kokoro
  → CUDA build, each with an `nvidia` device reservation, and raises the default Whisper
  model to `small`. Stacked on top of the base file, so CPU stays the safe default.
- **Hybrid override** (`docker-compose.hybrid.yml`): puts **only Kokoro** on the GPU and
  leaves Whisper on the base CPU image. This is the recommended mode with a GPU — Kokoro
  was the CPU bottleneck; Whisper never was, and its CUDA image is ~25 GB. Install defaults
  to hybrid when a GPU is present.
- **Whisper model cache** (`whisper-cache` named volume mounted at `/root/.cache`): the
  faster-whisper model persists across container recreates, so switching modes / restarting
  doesn't re-download it (~0.5 GB). Shared by the CPU and GPU whisper images (same path).
- **Compute axis in config** (`~/.voicemode-local/config`): `COMPUTE_MODE=cpu|gpu`,
  `WHISPER_MODEL=...`. Orthogonal to the existing routing modes (local/piper/hybrid/…).
- **Shared helper** (`lib/compute.sh`): `vm_detect_gpu` (checks GPU+driver, the nvidia
  docker runtime, and docker reachability), plus config read/write. Sourced by both
  `install.sh` and `voicemode-switch` so they agree.
- **`voicemode-switch compute`**: `show` prints mode + capability + images; `gpu`
  verifies the GPU, recreates containers on CUDA images, confirms a GPU process appears,
  and **rolls back to CPU** if they don't become healthy; `cpu` switches back.
- **Install-time selection** (`install.sh`): docker installs detect the GPU and offer
  GPU (recommended) vs CPU, then save the choice + best Whisper model.

Not done: native (non-Docker) Kokoro-ONNX GPU. The native `kokoro-onnx-server.py` path
stays CPU; the GPU work targets the Docker backends that actually run here. Piper stays
on CPU (tiny, already fast).

## How to Recreate

### Prerequisites
- NVIDIA GPU + driver (`nvidia-smi` works)
- `nvidia-container-toolkit` installed and the `nvidia` runtime registered with Docker
  (WSL2 + Docker Desktop provides this once GPU support is enabled). Verify:
  `docker info --format '{{json .Runtimes}}' | grep nvidia`

### Steps
1. `voicemode-switch compute` — see current mode and whether the GPU is usable.
2. `voicemode-switch compute hybrid` — recommended: Kokoro on GPU, Whisper on CPU
   (pulls the ~18 GB Kokoro CUDA image first time; recreates containers).
3. `voicemode-switch compute gpu` — both on GPU (also pulls the ~25 GB Whisper CUDA image).
4. `voicemode-switch compute cpu` — back to all-CPU.

Switching recreates containers (interrupts any in-flight exchange) and, on first use of a
GPU mode, allows up to 600 s for CUDA init / image build before concluding failure (and
auto-rolling back to CPU). The Kokoro GPU image (`kokoro-fastapi-gpu:v0.2.0`) exposes
`/health` and `/v1/audio/speech` but **not** `/v1/models` — health probes use `/health`.

### Verification
- `voicemode-switch compute` shows the active mode + image set.
- `nvidia-smi --query-compute-apps=process_name --format=csv` lists a python/kokoro/
  whisper process when GPU mode is live and a request is in flight.
- `voicemode-switch health` — all configured proxies up.
- `docker stats --no-stream voicemode-kokoro` — CPU mode stays at/under the cap.

## Gotchas

- **Switching recreates containers** → it drops active audio. It does *not* change
  routing, so no Claude Code restart is needed.
- **No `v0.1.5` GPU tag.** The `kokoro-fastapi-gpu` repo has no v0.1.5 (the CPU pin).
  Default GPU tag is `v0.2.0` (same OpenAI-compatible API + voicepack); override with
  `KOKORO_GPU_IMAGE` (e.g. `v0.1.4` for closest CPU parity, `v0.4.0` for newest CUDA).
- **Image-tag drift:** the running CPU container was on `:latest`, while the old compose
  pinned `:v0.1.5` (an image not present). The base file now defaults Kokoro to `:latest`
  to match what runs and avoid a surprise ~8 GB re-pull. Override via `KOKORO_IMAGE`.
- **`deploy.resources.limits.cpus`** is honored by `docker compose up` in Compose v2
  (it was Swarm-only in v1) — confirmed on v2.39.

## Configuration

`~/.voicemode-local/config`:

| Key | Values | Meaning |
|-----|--------|---------|
| `COMPUTE_MODE` | `cpu` \| `gpu` \| `hybrid` | Which image set + device reservations to use (hybrid = Kokoro GPU + Whisper CPU) |
| `WHISPER_MODEL` | `base`/`small`/`medium`/… | Whisper model (GPU default `small`, CPU `base`) |

Env overrides (compose interpolation): `KOKORO_CPUS` (CPU cap, default 6),
`KOKORO_IMAGE` (CPU Kokoro image), `KOKORO_GPU_IMAGE` (GPU Kokoro image).
