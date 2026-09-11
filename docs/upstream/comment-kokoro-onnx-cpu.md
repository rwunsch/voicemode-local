# Comment for upstream #262 — the CPU-only case you asked about

**Target:** [`mbailey/voicemode#262`](https://github.com/mbailey/voicemode/issues/262) (closed).
**Type:** comment on a closed issue, answering the maintainer's own invitation. Not a new issue.
**Submit as:** `gh auth switch --user rwunsch` first.
**Status:** POSTED 2026-09-11 as rwunsch — https://github.com/mbailey/voicemode/issues/262#issuecomment-5641877185

## Why this thread and not a new one

`ai-cora` closed #262 on 2026-06-25 with:

> "The one case mlx-audio doesn't cover is **CPU-only / non-Apple-Silicon (Linux/Windows)** where a
> fast ONNX Kokoro backend would still help. If that's your use case, please comment or reopen —
> that's the gap worth reconsidering an ONNX path for."

That is our case exactly. Opening a new issue would re-argue a decision already made and would skip
past @DiarmuidKelly's PR #261, which already did the work. Per this repo's own upstream rules:
attach to the open thread, ask before building.

---

## Draft comment

Answering the invitation in the closing note above — CPU-only, non-Apple-Silicon is our daily
driver, so here are numbers from it.

**Setup.** WSL2 on Windows 11, no Apple Silicon anywhere in our fleet. `kokoro-onnx==0.6.1` in a
clean venv, served through a small OpenAI-compatible shim, against `kokoro-fastapi` in Docker on the
same machine, capped at six cores to match what we run day to day. Voice `af_sky`, `response_format:
wav`, three fixed sentences, two runs each. Measured 2026-09-12.

| input | ONNX (CPU) | kokoro-fastapi (CPU) | kokoro-fastapi (GPU) |
| --- | --- | --- | --- |
| 1.5 s of audio | **0.52–0.60 s** | 0.71–0.82 s | 0.80 s |
| 4.9 s of audio | **1.39–1.48 s** | 1.82–2.42 s | 0.34 s |
| 13.7 s of audio | **3.54–4.10 s** | 5.67 s | 0.35 s |
| resident memory | **1.07 GB** | 1.50 GB | 1.42 GB |
| install size | **482 MB** (144 MB venv + 338 MB models) | ~2 GB native, 4.85 GB as an image | 18 GB image |

**Two honest caveats before the argument.**

First, we did **not** reproduce the "4.9x faster time-to-first-audio" from #261, and we are not
reusing that figure. We measured total synthesis time on a non-streaming server, which is a
different quantity, against a six-core torch baseline. Our result is a consistent but far more
modest 1.3x–1.6x.

Second, on a machine with a GPU this is not a close contest in ONNX's favour — kokoro-fastapi on
CUDA is roughly 10x faster on long input. We are not proposing ONNX as a replacement for that path,
and we will keep running the GPU one ourselves.

**The argument is only about machines without a GPU, and there it is one-sided:** faster, ~30% less
memory, 4x–10x less disk, and — the part we think matters most for Windows — **no system
`espeak-ng`**, because `kokoro-onnx` depends on `espeakng-loader` and bundles it. No Docker, no CUDA
toolkit, no build step.

That last point is why we think this fits voicemode's design rather than fighting it. The reasons to
build natively rather than ship containers — owning the service lifecycle, no Docker dependency, no
VM boundary — all still hold for an ONNX backend. It is a pip install and a model file. And it
sidesteps a class of friction this repo has already had to absorb on the native path: #250 (better
UX when the CUDA toolkit is missing during a whisper install) and #319 (a Nix derivation, added
because the build failed) are both build-environment problems that an ONNX TTS backend simply does
not have.

**The ask, before any code.** Would an in-tree ONNX Kokoro provider be welcome, in the shape
Cartesia established in 8.8.0 — auto-detected from a `VOICEMODE_TTS_BASE_URLS` entry, inert when not
configured, and not touching the existing kokoro-fastapi path at all? If so we are happy to build on
@DiarmuidKelly's #261 rather than start over, with credit to it. If the answer is that this belongs
out-of-tree and the URL-based routing is the intended answer, that is a perfectly good answer too and
we will document it that way on our side.

Related: #535 asks the same underlying question for Piper and native non-English voices. If the
answer there is "out-of-tree", it is probably the same answer here, and we would rather hear it once
than ask twice.

---

## Appended to the posted comment, 2026-09-12

A marked edit was added rather than a second comment, since the maintainer had not yet replied and a
follow-up an hour later reads as bumping. It reports the listening test: five matched A/B pairs
across four voices, no audible difference, with the scope stated (one listener, not blind) and the
reason it was unsurprising (kokoro-onnx converts the same Kokoro v1.0 weights).

## What we are deliberately not saying

- ~~No claim about quality.~~ **Answered 2026-09-12:** five pairs, four voices, no audible
  difference. One listener, not blind.
- No claim about concurrency. Our own reason for putting Kokoro on a GPU was that CPU synthesis
  starves the real-time audio pipeline when several sessions speak at once — a single-request
  benchmark is blind to exactly that.
- No claim that the int8 model behaves as #261 reports. We tested fp32 only.
