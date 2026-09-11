# ONNX Kokoro against torch Kokoro — measured

**All numbers below were produced on this machine on 2026-09-12.** Nothing here is repeated from
upstream PR #261 or from this repo's earlier notes. Where our results differ from #261's, ours are
stated and theirs is not reused.

## Setup

- Machine: WSL2 on Windows 11, 31.3 GB RAM visible to Linux, NVIDIA GPU present.
- ONNX: `kokoro-onnx==0.6.1` in a clean venv (`.venv-onnx`, Python 3.12), served by this repo's
  `kokoro-onnx-server.py` on `:8882`, model dir `models/kokoro`.
- torch CPU: `ghcr.io/remsky/kokoro-fastapi-cpu:latest`, `--cpus 6` (matching our `KOKORO_CPUS`
  default), on `:8883`.
- torch GPU: the live `voicemode-kokoro`, `ghcr.io/remsky/kokoro-fastapi-gpu:v0.2.0`, on `:8880`.
- Voice `af_sky` throughout, `response_format: wav`, three fixed sentences producing 1.5 s, 5 s and
  13.7 s of audio.
- Two runs per engine; the figures below are the range across both, so the first (cold) run is
  visible rather than discarded.

## Synthesis time

| input | ONNX CPU | torch CPU | torch GPU |
| --- | --- | --- | --- |
| short (1.5 s of audio) | **0.52–0.60 s** | 0.71–0.82 s | 0.80 s |
| medium (4.9 s) | **1.39–1.48 s** | 1.82–2.42 s | 0.34 s |
| long (13.7 s) | **3.54–4.10 s** | 5.67 s | 0.35 s |

## Memory and disk

| | ONNX CPU | torch CPU | torch GPU |
| --- | --- | --- | --- |
| resident while serving | **1.07 GB** | 1.50 GB | 1.42 GB |
| disk | **482 MB** (144 MB venv + 338 MB models) | ~2 GB native, or 4.85 GB image | 18 GB image |
| Docker required | no | yes (or a ~2 GB native build) | yes |
| CUDA toolkit / build step | no | no / yes | yes |
| system `espeak-ng` | no — `espeakng_loader` is bundled (21 MB) | required by the native path | required by the native path |

## What this says

**Against torch on CPU — the case upstream asked about — ONNX wins on every axis:** 1.3x to 1.6x
faster, about 30% less resident memory, 4x to 10x less disk, no Docker, no build, no system espeak.

**Against torch on a GPU it is not close:** 10x slower on long input (3.54 s against 0.35 s). On a
workstation with a GPU, the Docker GPU path remains the better choice for responsiveness, and we
should keep it here.

**We did not reproduce PR #261's "4.9x faster time-to-first-audio".** That is not a contradiction of
their result — we measured *total synthesis time on a non-streaming server*, which is a different
quantity from time-to-first-audio, and our torch baseline ran on six cores rather than one. The
honest statement upstream is our own 1.3x-1.6x on total synthesis, with the metric named.

## Voice parity

Audio durations for the same text were within 5% between engines (13.70 s ONNX against 14.48 s
torch on the long sentence).

**Listening test, 2026-09-12 — no audible difference.** Robert compared five matched A/B pairs
(`af_sky`, `am_puck`, `bm_lewis`, `af_heart` on an acronym-heavy sentence, plus `af_sky` on a longer
natural one) and judged them **identical in all cases**. Player and samples:
`C:\Temp\voice-compare\listen.html`.

Scope of that result, stated honestly: **one listener, five pairs, four voices.** Not a blind test,
and not a formal MOS. All seven session voices (`af_sky`, `am_puck`, `bm_lewis`, `af_heart`,
`bf_alice`, `af_river`, `am_eric`) were confirmed to *exist and synthesize* in the ONNX model
(HTTP 200), but only four were listened to.

Identical output is the expected result rather than a surprise — kokoro-onnx is a conversion of the
same Kokoro v1.0 weights, not a different model. The test was worth running because a conversion
*can* degrade, not because degradation was likely.

## Not tested

- The int8 model, which is where #261's 88 MB figure comes from.
- Concurrent sessions. Our whole reason for putting Kokoro on the GPU was that CPU synthesis starves
  the real-time audio pipeline under several sessions at once; a single-request benchmark cannot see
  that, and it is the most likely way ONNX-on-CPU disappoints in real use.
- macOS and native Windows.

## Commands

```bash
# environment
uv venv .venv-onnx --python 3.12 && VIRTUAL_ENV=.venv-onnx uv pip install kokoro-onnx
.venv-onnx/bin/python kokoro-onnx-server.py --port 8882 --model-dir models/kokoro

# torch CPU baseline
docker run --rm -d --name kokoro-cpu-bench --cpus 6 -p 8883:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest

# one timing sample
curl -s -o out.wav -X POST http://127.0.0.1:8882/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"kokoro","input":"<text>","voice":"af_sky","response_format":"wav"}'
```
