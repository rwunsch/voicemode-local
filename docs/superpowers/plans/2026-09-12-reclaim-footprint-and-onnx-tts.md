# Reclaim the dead install footprint, and test whether ONNX can replace the torch TTS

**In one paragraph, for someone who wasn't there.** Running voice locally on this machine costs
about 58 GB of disk. When we actually measured what each part was doing, roughly 26 GB of that
was serving nothing at all — a GPU Whisper image kept by a compute-mode setting nobody revisited, a
Piper container superseded months ago by an in-process proxy, and a 7 GB Kokoro install abandoned
when the setup moved to Docker. None of it is a bug; each piece was correct when it was made and
then outlived its reason. This plan removes the dead weight, and then tests something bigger: the
local text-to-speech engine drags in PyTorch, CUDA wheels and Japanese dictionaries to run voice
models that are 516 KB each. There is an ONNX version that should do the same job in a fraction of
the space. If the measurements hold, we switch to it here and offer it upstream — where a
maintainer has already asked for exactly this, for exactly our platform.

## Goal

1. Reclaim ~26.3 GB with no functional change. *(Originally written as 34.5 GB — see the correction
   under Task 1.)*
2. Establish, by measurement on this machine, whether ONNX Kokoro is a viable replacement for the
   Docker/torch Kokoro for day-to-day use.
3. If it is, offer it upstream in the place a maintainer invited it, and make it the local default.

## Decisions

**D1 — We comment on the existing issue #262; we do not open a new one.**
Upstream PR #261 (DiarmuidKelly, 2026-02-13) already implemented an ONNX Kokoro backend and was
closed unmerged on 2026-06-16. Issue #262 was closed on 2026-06-25 as superseded by mlx-audio,
which is Apple Silicon only. The maintainer's closing comment says, verbatim:

> "The one case mlx-audio doesn't cover is CPU-only / non-Apple-Silicon (Linux/Windows) where a
> fast ONNX Kokoro backend would still help. If that's your use case, please comment or reopen —
> that's the gap worth reconsidering an ONNX path for."

That is an open invitation naming our exact platform. A fresh issue would re-argue a closed
decision and ignore Diarmuid's work. This also follows rules 1 and 4 of
[`../../upstream/README.md`](../../upstream/README.md): attach to an open issue, and ask before
building anything net-new.

**D2 — The proposal is framed as native, never as Docker.**
[`why-upstream-builds-from-source.md`](../../upstream/why-upstream-builds-from-source.md) concluded
that upstream avoids Docker because macOS is its primary platform and Docker there loses Metal, and
because upstream wants to own the service lifecycle. The ONNX path agrees with all of that: it is a
Python package plus a model file, it needs no Docker, no CUDA toolkit and no build from source, and
`kokoro-onnx` depends on `espeakng-loader`, so it needs no system `espeak-ng` either. The argument
to make is that ONNX is *more* aligned with upstream's stated philosophy than the torch path, not
less.

**D3 — No upstream PR until a maintainer replies.** Upstream merges roughly 5% of external PRs and
`master` has had no public merge since 2026-07-21. An unsolicited feature PR is a donation to the
archive. Same courtesy pattern as issue #535.

**D4 — Nothing here may break day-to-day voice.** The Docker Kokoro path stays installed and
reachable until ONNX has been measured and used for real work. Reverting is a one-line change to
`VOICEMODE_TTS_BASE_URLS`.

## What was measured (2026-09-12, this machine)

| item | size | status |
| --- | --- | --- |
| `onerahmet/openai-whisper-asr-webservice:latest-gpu` | 25.7 GB | running only because `COMPUTE_MODE=gpu` |
| `~/.voicemode/services/kokoro` | 7.0 GB | dead — `INSTALL_MODE=docker`, no process, untouched since 2026-07-02 |
| `rhasspy/wyoming-piper` | 1.83 GB | dead — nothing consumes `:10200`; `piper-proxy.py` loads `PiperVoice` in-process |
| `ghcr.io/remsky/kokoro-fastapi-gpu:v0.2.0` | 18 GB | live, serving TTS on `:8880` |
| `ghcr.io/remsky/kokoro-fastapi-cpu` | 4.85 GB | idle spare for `compute cpu` |

Inside the dead 7.0 GB: nvidia CUDA wheels 2.7 GB, torch 1.5 GB, triton 685 MB, Japanese and
Chinese dictionaries 587 MB, spacy 101 MB. The Kokoro voice models it exists to run are 516 KB each.

**Correction to two numbers used earlier in this investigation**, both now measured rather than
quoted:

- The 7 GB is the **GPU** variant. Upstream's installer defaults to `start-gpu.sh` on Linux; the CPU
  path pins `torch==2.6.0` from the pytorch-cpu index (179 MB wheel against 768 MB for CUDA) and
  skips nvidia and triton, so a CPU install is roughly **2 GB**, not 7.
- The light path is **not** ~92 MB, and that figure in `CLAUDE.md` was never validated —
  `kokoro_onnx` is not installed here and the model files are not on disk, so it has never run.
  Measured downloads are `kokoro-v1.0.onnx` **326 MB** and `voices-v1.0.bin` **28 MB**; with
  onnxruntime at 61 MB the honest figure is about **415 MB**. The 88 MB in PR #261 is the **int8**
  model, which we have not tested.

So the claim to defend upstream is 415 MB against 2 GB (CPU) or 6.6 GB (GPU) — a 5x to 16x
reduction, not 75x.

## Global constraints

- Every number that reaches upstream is measured on this machine, with the command recorded. No
  figure is repeated from PR #261 or from this repo's own notes without re-deriving it.
- Deletions are reversible in principle: images can be re-pulled, and the dead Kokoro tree is
  re-creatable by upstream's installer. Record what was removed and how to restore it.
- Submit as `rwunsch` (`gh auth switch --user rwunsch`), never the work account.

---

## Task 1 — Reclaim the dead weight

**Corrected 2026-09-12, during execution: the reclaim is ~26.3 GB, not 34.5 GB.** The replacement
CPU Whisper image is **8.19 GB**, not the small thing "lean CPU image" implied everywhere in these
notes — including in `docker-compose.hybrid.yml`'s own header, where "lean" was meant relative to
25.7 GB rather than as an absolute. So the Whisper swap nets 25.7 − 8.19 = **17.5 GB**, and the
total is 17.5 + 1.83 (Piper) + 7.0 (dead Kokoro tree) = **26.3 GB**. The error was mine: I treated a
comparative adjective as a measurement and never checked the image size until it had downloaded.

- [x] Record the current state first: `docker images`, `docker ps`, `du -sh ~/.voicemode`,
      and a copy of `~/.voicemode-local/config`, into `artifacts/2026-09-12-footprint-before.txt`.
- [x] Switch compute mode to hybrid: `voicemode-switch compute hybrid`. This moves Whisper back to
      the lean CPU image and leaves Kokoro on the GPU, which is what
      `docker-compose.hybrid.yml` was written for.
- [x] Confirm STT still works before deleting anything — one `converse` round trip, and check the
      whisper container is the CPU image.
- [~] Measure STT latency with `WHISPER_MODEL=small` on CPU. **Not done, and no longer needed:**
      `voicemode-switch compute hybrid` reset `WHISPER_MODEL` from `small` to `base` itself, so
      there was never a `small`-on-CPU state to measure. `base` on CPU transcribes 4.9 s of audio in
      **0.78 s**, verbatim correct, which settles the concern that prompted the step.
- [x] Remove the GPU Whisper image: `docker image rm onerahmet/openai-whisper-asr-webservice:latest-gpu`
      (~25.7 GB).
- [x] Stop and remove the Piper container, and set `PIPER_ENABLED=false`; remove
      `rhasspy/wyoming-piper` (~1.83 GB). German and other Piper voices are unaffected — they are
      served by `piper-proxy.py` on `:8881` from the in-process `PiperVoice`, which never used the
      container.
- [x] Verify a German voice still speaks after the container is gone.
- [ ] Delete the dead native Kokoro install: `rm -rf ~/.voicemode/services/kokoro` (~7.0 GB).
- [x] Record the after state into `artifacts/2026-09-12-footprint-after.txt` and confirm the delta.

## Task 2 — Make the ONNX path real and measure it

- [x] Install `kokoro-onnx` into the working venv and start `kokoro-onnx-server.py` on a free port
      (not `:8880`, which Docker Kokoro holds, and not `:8881`, which Piper holds — see
      [`../../kokoro-port-collision/README.md`](../../kokoro-port-collision/README.md)).
- [x] Record what it actually downloads and what the install weighs: model, voices, onnxruntime,
      total on disk.
- [x] Measure, on the same three sentences, against Docker Kokoro on `:8880`: time to first audio,
      total synthesis time, and resident memory. Short, medium and long input. Record the exact
      commands.
- [ ] Check voice parity: do the voices we actually use (`af_sky`, `am_puck`, `bm_lewis`,
      `af_heart`) exist and sound equivalent through the ONNX server?
- [ ] Test the int8 model as a second data point, since that is the 88 MB figure PR #261 quotes.
- [x] Write the results into `artifacts/2026-09-12-onnx-vs-torch.md` — numbers, commands, sample
      size, and what was not tested.

## Task 3 — Comment on upstream #262

- [x] Draft `docs/upstream/comment-kokoro-onnx-cpu.md` following the house format: lead with the
      mechanism, quote the maintainer's own invitation back, credit PR #261, give our measured
      Linux/WSL numbers, and ask whether an in-tree ONNX provider would be welcome before offering
      one. Cross-link #535, which asks the same underlying question about Piper.
- [x] Add it to the queue table in `docs/upstream/README.md`.
- [x] `gh auth switch --user rwunsch`, post the comment on #262, then switch back.
- [x] Record the issue URL and the date in the queue table.

## Task 4 — Only if the measurements hold, and only after a reply

- [ ] Make ONNX the default local TTS here: first entry in `VOICEMODE_TTS_BASE_URLS`, with Docker
      Kokoro demoted to fallback rather than removed.
- [ ] Live with it for a week of normal work before removing the 18 GB GPU Kokoro image.
- [ ] Update `CLAUDE.md` (the services table, the compute-mode section, and the unvalidated 92 MB
      figure) and `docs/compute-modes/README.md`.
- [ ] If upstream welcomes it, open the PR — one change, native only, no Docker.

## What could make this wrong

- **ONNX quality may not match torch** on the voices we use daily. That is a listening test, not a
  benchmark, and it is the most likely reason to stop at Task 2.
- **The GPU Kokoro may genuinely be faster** than ONNX on CPU for long utterances. If so the
  honest outcome is "ONNX for portability, GPU for this machine", and the upstream argument
  narrows to machines without a GPU — which is still the case the maintainer asked about.
- **A maintainer may simply not reply.** #535 has had no answer in six days. The local benefit does
  not depend on upstream, and Task 4's local half proceeds regardless.
