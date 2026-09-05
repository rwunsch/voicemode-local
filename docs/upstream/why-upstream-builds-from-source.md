# Why upstream builds whisper/kokoro from source instead of shipping Docker

A decision record, so this isn't relitigated. **We do not upstream our Docker stack.**

## What's actually there

**Validated** (read from `mbailey/voicemode` master and voice-mode 8.12.0, 2026-09-05):

- **Zero Docker artifacts upstream.** No `Dockerfile`, no compose file, no Docker mention in
  docs. A deliberate absence, not an oversight.
- `tools/whisper/install.py` clones `ggerganov/whisper.cpp`, detects GPU, builds with cmake,
  downloads/benchmarks/selects models (`model_install.py`, `model_benchmark.py`,
  `model_active.py`).
- `tools/kokoro/install.py` installs kokoro-fastapi via `uv`.
- `tools/service.py` (~900 lines) manages both through **systemd user units** on Linux and
  **launchd plists** on macOS, with templates shipped in the package.

## Why (strongest reason first)

**1. macOS is the primary platform, and Docker breaks GPU there.** *(Inference from
observed facts, not a maintainer statement.)* The evidence that macOS leads: launchd plists
shipped alongside systemd units; Hammerspoon media-key scripts; `mlx_audio` as an
Apple-Silicon TTS backend; an open PR for MLX Parakeet STT. On macOS, Docker Desktop is a
Linux VM — **no Metal passthrough**. A Dockerised whisper.cpp or Kokoro on a Mac loses
hardware acceleration entirely, while a native build gets Metal. For a real-time voice
pipeline that is not a tuning detail, it's the difference between usable and not.

**2. Upstream wants to own the lifecycle.** `voicemode service {install,start,stop,enable,
logs}` is a uniform interface across both platforms. That only works if voice-mode controls
the process. Under Docker, lifecycle belongs to another daemon: `Restart=always` (VM-1398,
"so kokoro survives UVICORN...") isn't expressible, `service logs` has nothing to read, and
`service enable` can't guarantee start-on-login.

**3. Latency.** *(Inference.)* The project tunes hard for sub-second response — 8.8.0's
Cartesia integration advertises "audio starts playing within a few hundred milliseconds".
Docker adds a network hop, and on macOS a VM boundary.

**4. Install-size asymmetry.** The Whisper CUDA image is ~25GB (measured on this machine,
which is why our own default is hybrid: Kokoro on GPU, Whisper on the lean CPU image). A
native whisper.cpp build is a fraction of that.

**5. One less hard dependency.** Requiring Docker Desktop for a voice assistant is a heavy
ask on a laptop, and it's licensed for commercial use at some org sizes.

## So why do *we* use Docker?

Different constraints, and they're good ones:

- **WSL2, not macOS.** No Metal to lose; CUDA passes through to WSL2 fine.
- **Reproducibility across three OSes** without compiling whisper.cpp on each.
- **Compute-mode switching** (`voicemode-switch compute gpu|hybrid|cpu`) is trivially
  expressible as stacked compose files and awkward as native builds.
- **CPU capping.** `KOKORO_CPUS` stops Kokoro starving the audio pipeline — a container
  limit, essentially free; a native cgroup, not.

Both choices are right for their platform. That's why this stays a fork-level difference
rather than a PR.

## What *is* worth upstreaming

Not the stack — the collision it causes. A Docker backend on `:8880` and an enabled
`voicemode-kokoro.service` fight for the port; on one machine that produced 16,174 failed
unit starts in 14 days. See [`issue-service-foreign-backend.md`](issue-service-foreign-backend.md).
The fix is for `service status` to notice a healthy backend it didn't start — which needs
no knowledge of Docker at all, and so doesn't ask upstream to change its mind about anything.
