# German Kokoro Voices

> **TL;DR:** Upstream Kokoro v1.0 has **no German voices**. A voicepack only works with the weights it
> was trained with, so German needs a separately fine-tuned model, not just a voice file. The
> best fit for this repo is **Kokoro-82M-ONNX-German-Martin**: same ONNX graph as v1.0, so
> `kokoro-onnx-server.py` loads it as a second model and serves it as voice **`dm_martin`** on the
> existing `:8880` endpoint.

| Field        | Value                        |
|--------------|------------------------------|
| Status       | Complete                     |
| Created      | 2026-10-19                   |
| Last Updated | 2026-10-19                   |
| Project      | voicemode-local (macOS)      |

## Why

We wanted German TTS through voicemode with Kokoro quality. Piper (`:8881`) already serves German
(`p_de_thorsten`, `p_de_eva`, …), but Kokoro sounds more natural.

## Findings

### 1. The stock voicepack has no German

`models/kokoro/voices-v1.0.bin` holds 54 voices. Prefixes: `a` (US English), `b` (UK English), `e` (Spanish),
`f` (French), `h` (Hindi), `i` (Italian), `j` (Japanese), `p` (Portuguese), `z` (Mandarin). There is
no `d`/`g` prefix. Upstream Kokoro only shipped languages that had enough permissively licensed
training data and a G2P frontend, and German was not one of them.

### 2. Workaround without new files: stock voices + German phonemes

`kokoro_onnx.Kokoro.create(..., lang="de")` phonemizes with espeak-ng German, and any stock voice
then reads it. We tested `af_heart`, `bm_george`, `ff_siwis` and `im_nicola`. All were intelligible,
and all had a clear foreign accent. Usable as a fallback.

### 3. "Just add the voice" does not work

A Kokoro voice is a style embedding learned jointly with the model weights. A German fine-tune
changes the weights, so its voicepack is meaningless on the stock `kokoro-v1.0.onnx`, and the reverse
is also true. You need the model file plus its voicepack, not a whole server fork.

### 4. German Kokoro models that exist (as of 2026-10)

| Model | Voices | Format | Fit for this repo |
|-------|--------|--------|-------------------|
| [Godelaune/Kokoro-82M-ONNX-German-Martin](https://huggingface.co/Godelaune/Kokoro-82M-ONNX-German-Martin) (mirror: [huggingFresse/…](https://huggingface.co/huggingFresse/Kokoro-82M-ONNX-German-Martin)) | 1 male (Martin) | ONNX (`kokoro-martin.onnx`, 325 MB, same size as v1.0) + `voices-martin.npz` | **Drop-in** for `kokoro-onnx`. Ships its own FastAPI server (default port **8881**, which collides with our Piper) and German text normalization, which we do not use (yet) |
| [Thorsten-Voice/Kokoro](https://huggingface.co/Thorsten-Voice/Kokoro) | 1 male (Thorsten) | PyTorch `.pth` + `.pt` voicepack, no ONNX | Needs PyTorch and patched kokoro/misaki forks, or an ONNX export first |
| [r1di/kokoro-fastapi-german](https://github.com/r1di/kokoro-fastapi-german) | `df_eva`, `dm_bernd` | Full Kokoro-FastAPI fork, "Kokoro-German v1.1" | Two voices, but it replaces the whole server. Small fork (2 stars) |

The Thorsten fine-tune was trained with the kikiri-tts recipe by semidark (formerly
`kokoro-deutsch`).

## Integration

`kokoro-onnx-server.py` now lazily loads an optional second model from
`<model-dir>-de-martin/` (override with `--de-model-dir`):

```bash
# one-time download (~325 MB)
.venv/bin/python kokoro-onnx-server.py --model-dir models/kokoro --download-german
```

- Request voice `dm_martin` → German model, `lang="de"`.
- Every other voice → stock model, `lang="en-us"` (unchanged behaviour).
- If the German files are missing, `dm_martin` returns HTTP 400 with the download hint; nothing
  else changes.
- `install.sh` (step 7a) downloads the German model by default; the macOS manual steps in the
  README list the same command.
- Docker mode (Kokoro-FastAPI container) does **not** get `dm_martin`. Use Piper's German voices
  there.

Use it from voicemode with `voice="dm_martin"` on `converse`. For German *input*, Whisper must also
transcribe German, which it auto-detects.

## Test log

- 2026-10-19: Stock voices with `lang="de"`: intelligible, accented (finding 2).
- 2026-10-19: Martin model via `kokoro-onnx-server.py` on :8880, `converse(voice="dm_martin")`:
  natural German with no accent. User verdict: "eine klasse deutsche Stimme". English voices unchanged.
- Martin's voicepack holds a single key, `martin` (shape 510×1×256). The server maps
  `dm_martin` to it.

## Keeping German voices German

`dm_martin` phonemizes everything as German, so English text comes out garbled. This happened
on 2026-10-19 when the agent kept Martin while answering in English. Two guards:

- `patches/patch_voice_language.py` adds a LANGUAGE-LOCKED VOICES note to the `converse` tool
  description, so every MCP client sees it. Applied by `patches/apply.sh`, and needs a Claude Code
  restart to take effect.
- CLAUDE.md rule for sessions inside this repo.

## Open ideas

- Port `german_text_rules.py` / `tts_normalizer.py` from the Martin repo (numbers, dates, units,
  €) into the server for `dm_martin`.
- Export Thorsten-Voice/Kokoro to ONNX for a second German voice.
