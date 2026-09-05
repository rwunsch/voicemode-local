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

## Revised 2026-09-05: there IS a case for Docker as an *optional* path

The position above ("don't upstream the stack") was right about not proposing Docker as a
**replacement**. It was too absolute about proposing it as an **alternative**. The evidence
that changed this is in upstream's own tracker: building whisper.cpp from source is a
recurring, cross-distro support burden.

**Validated — build/install pain in upstream's own issues and PRs:**

| Item | What it is |
|---|---|
| #250 (merged 8.8.0) | "Clearer guidance when the CUDA toolkit is missing during a GPU whisper install" — a *build* failure needing distro detection (apt vs dnf) |
| #319 (merged 8.8.0) | NixOS flake, because the install tool gave "a cryptic FHS build failure" |
| #517 (open since 2026-08-13) | Fedora Atomic (Silverblue / Bazzite / Bluefin) support — immutable distros where compiling is actively hostile |
| #524 (open since 2026-08-18) | "native Windows support for whisper/kokoro install and start" |

Four separate items, all downstream of "you must compile whisper.cpp on the user's machine".
A prebuilt container eliminates that entire class on Linux, WSL and Windows.

**Real advantages of the Docker path, honestly stated:**

1. **No build toolchain.** No cmake, no compiler, no CUDA toolkit. This is the big one, and
   the four items above are the receipts.
2. **Immutable and atomic distros work unchanged** — no FHS assumptions, no `dnf` at all.
3. **Resource capping is free.** `KOKORO_CPUS` stops TTS starving the real-time audio
   pipeline; a container limit is one line, a native cgroup is not.
4. **Compute-mode switching is declarative** — GPU / hybrid / CPU as stacked compose files.
5. **Clean uninstall** — remove containers and volumes; nothing left in `~/.voicemode`.
6. **Pinned, reproducible runtime** across machines and CI.

**And the disadvantages remain real, so this must stay opt-in:**

1. **macOS loses Metal.** Docker Desktop is a Linux VM with no GPU passthrough, so a
   containerised whisper/Kokoro on a Mac is materially slower than a native Metal build.
   Since macOS is upstream's primary platform, Docker must never become the default.
2. **Docker Desktop is a heavy dependency** and is licensed for commercial use at some org
   sizes.
3. **The Whisper CUDA image is ~25GB** (measured here — which is why our own default is
   hybrid: Kokoro on GPU, Whisper on the lean CPU image).
4. **Lifecycle moves to another daemon** — `service logs`, `Restart=always` and
   start-on-login all assume voice-mode owns the process.

**So the proposal shape is:** not "switch to Docker", but *"an optional, documented
container path for Linux / WSL / Windows, for users who would rather not build whisper.cpp"* —
alongside the native path, never replacing it, and explicitly not recommended on macOS.
That is a far more defensible PR than the one ruled out above, and it answers four existing
issues rather than changing anyone's mind.

It also depends on the service-awareness fix below, since the two paths collide on `:8880`.

## What *is* worth upstreaming

Not the stack — the collision it causes. A Docker backend on `:8880` and an enabled
`voicemode-kokoro.service` fight for the port; on one machine that produced 16,174 failed
unit starts in 14 days. See [`issue-service-foreign-backend.md`](issue-service-foreign-backend.md).
The fix is for `service status` to notice a healthy backend it didn't start — which needs
no knowledge of Docker at all, and so doesn't ask upstream to change its mind about anything.
