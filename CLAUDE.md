# VoiceMode Local

Local voice services for Claude Code, with Whisper STT, Kokoro TTS, and Piper TTS.

## Supported Environments

- **WSL2 on Windows 11** (primary, tested on Ubuntu 22.04 LTS)
- **Ubuntu 22.04+** (native Linux, should work with PulseAudio/PipeWire)
- **macOS** (partially tested — native mode: whisper.cpp via `WHISPER_CPP_MODEL`, Kokoro ONNX, Piper; see README "macOS (native, no Docker)")
- **Windows (native)** — work in progress, see below. Native STT is whisper.cpp from a pinned zip: the CUDA build when `python -m vml_supervisor.gpu` (the one shared GPU check: nvidia-smi, a GPU, driver >= 527.41 on Windows and WSL or >= 525.60.13 on Linux) says available (`VOICEMODE_WHISPER_GPU=auto|on|off`; interactive setup asks "Use the NVIDIA GPU for speech recognition? (about 2 GB more) [Y/n]" and writes on/off to voicemode.env, `setup --no-questions` / a non-interactive install.ps1 keeps auto), checked on the GPU by setup, which fails with `whisper_gpu_failed` rather than falling back to the CPU without saying so. Median 0.21 s vs 6.94 s on the CPU for a 7.6 s clip (n=5 each, 2026-11-17); see docs/windows-native/README.md

## Windows Working Copy

A second checkout lives at `C:\git\voicemode-local` (from WSL: `/mnt/c/git/voicemode-local`) holding the Windows port work on branch `feature/piper-integration` (commit `c03c099`, pushed but not merged to main). It adds `install.ps1` (modes: `wsl-mcp` / `wsl-shared` / `windows-native`), `voicemode-services.ps1`, Windows stdlib shims (`patches/fcntl_shim.py`, `patches/resource_shim.py`), and `docs/windows-issues.md` — a catalogue of 6 upstream voice-mode/Claude Code issues blocking native Windows. The `wsl-mcp` bridge mode works; true native mode is blocked mainly by issue #5 (recording loop hangs when sounddevice callbacks starve). When editing files in that copy, expect CRLF line endings.

## Voice Selection on Startup

When starting a voice conversation via `/voicemode:converse`, offer voice selection before the first message:

> "Starting voice mode. Want me to pick a random voice for this session, or use the default (af_sky)? You can also name a specific voice."

- **Random voice**: Pick from this pool of distinct-sounding voices (excludes current default):
  `af_bella`, `af_heart`, `af_nova`, `am_adam`, `am_eric`, `am_puck`, `bf_emma`, `bm_daniel`, `bm_george`, `ff_siwis`, `if_sara`, `im_nicola`
- **Named voice**: Use it directly as the `voice` parameter
- **German**: Piper's `p_de_thorsten`, or `dm_martin` only where the German Kokoro model is installed (`kokoro-onnx-server.py --download-german`, a third-party Hugging Face model; not installed by default, and then dm_martin fails on every server). Stock English Kokoro voices mispronounce German
- **German voices speak German only.** With `dm_martin` (or any `p_de_*` voice) write every `message` in German, even if the user speaks English. The model phonemizes everything as German, so English text comes out garbled. If the user wants English, switch to an English voice instead
- **Default / "just start"**: Proceed without specifying a voice
- **A voice that is not installed** is spoken with an installed local voice of the same language (else the first installed `VOICEMODE_VOICES` entry), never OpenAI; the converse result then says "Voice X is not installed here; spoke with Y." (`patches/voice_fallback.py`)
- **Echo labels**: `VOICEMODE_ECHO_ASSISTANT_LABEL` / `VOICEMODE_ECHO_USER_LABEL` (default ASSISTANT / USER) set the names in converse's `> **X (voicemode):**` echo lines; read at voice-mode start
- **Speech recognition follows the pill's language**: with a language chosen in the pill, the session's STT request passes its ISO code (de, en, ...) so Whisper does not guess; unset, auto-detect / `VOICEMODE_WHISPER_LANGUAGE` as before

Pass the selected voice as the `voice` parameter on every `mcp__voicemode__converse` call for that session.

**Also set a session label** so concurrent voice sessions are distinguishable when they
hand off the mic. At voice-mode start, derive a short 1–3 word label for what this session
is about (e.g. `queue-naming`, `windows-port`); fall back to the repo name if nothing
specific stands out. Write it once:

```bash
mkdir -p ~/.voicemode/session_names && \
  printf '%s' '<label>' > ~/.voicemode/session_names/"$CLAUDE_CODE_SESSION_ID".txt
```

This label is what a session announces on handoff ("This is <label>, <voice> —") and is
spoken only when another session is waiting. If the user later says "call this session X",
rewrite the file with the new label — it takes effect on the next exchange, no restart.

## Switching Voices Mid-Conversation

If the user asks to change voice during a conversation, switch the `voice` parameter on the next `mcp__voicemode__converse` call. Acknowledge the switch briefly.

## Session Queue (multiple concurrent voice sessions)

Concurrent Claude Code sessions take strict FIFO turns on the voice channel.
Since 2026-09-05 this is **upstream voice-mode's conch queue** (8.8.0, epic
VM-1610), not our own `voice_queue.py` — that was deleted because upstream built
the same design, and better: order is allocated under an flock'd counter rather
than a microsecond clock, so it stays correct across machines.

Three things to know when using `mcp__voicemode__converse`:

1. **`wait_for_conch`** is the gate. Left false (the default), a busy channel
   does not queue you at all. Pass `wait_for_conch=true` to join the FIFO
   waiter queue — you then appear in `voicemode conch status`. It fast-fails if
   the holder dies, so you cannot be stuck behind a corpse.
2. **`conch_mode`** decides how you are served once queued. `"wait"` (the
   default) blocks until the floor is granted to you; prefer it. `"callback"`
   returns immediately — your message is **not** spoken at call time — and you
   are woken when your turn comes, then take the floor by calling converse
   again. Upstream can only wake through tmux, so `patches/conch_wakers.py`
   (2026-11-11) wakes per harness: `VOICEMODE_CONCH_WAKE_COMMAND`, Claude Code's
   session socket (`session_wake.py`, POSIX), or upstream's tmux. **Where none
   can reach the session, callback is served as wait** (FELIX/Copilot today), so
   an unwoken callback can no longer block the queue. A woken session that stays
   away past 10 s may be passed and keeps its place; a busy one is skipped and
   re-woken (max 5). Validated live on WSL with Claude Code, 2026-11-11 (n=1 per case). Never downgrade a queued
   question to text, and never give up.
3. **`hold_conch=true`** keeps the floor *across turns* — use it when your next
   converse call continues the same thread (asking a question you will answer,
   or speaking across several turns) so another agent cannot cut in mid-thought.
   It is a short, refreshed TTL (10s idle, re-stamped each turn); override per
   call with `conch_hold_timeout`. Release by simply not passing it next turn.
   **While another session waits, a hold is limited** (`patches/conch_hold.py`,
   2026-11-11): a held turn that got no reply releases the mic, and at most 3 held
   turns in a row keep it (`VOICEMODE_CONCH_HOLD_MAX_TURNS`).

Long waits and listens send MCP progress every 30 s (`patches/mcp_progress.py`,
`VOICEMODE_PROGRESS_INTERVAL`, 0 = off), so a client with a 180 s tool timeout
(Copilot CLI) does not cancel converse. It is sent only when the client asks for
progress; the voice server logs "client token present/absent" per call.

There is no `end_burst` and no `ticket` any more — those were our queue's API.
Handing the mic on is now the *default*: the floor releases at the end of your
turn unless you asked to hold it.

Inspect and drive the queue:

```bash
./voicemode-switch queue          # -> voicemode conch status (holder + waiters)
./voicemode-switch floor reset    # -> voicemode conch release
./voicemode-switch conch give <session>   # hand the floor to a named session
```

Sessions are named by `patches/patch_session_name.py` — upstream hardcodes every
holder as "converse", so without it `conch status` cannot tell your sessions
apart. The label comes from `VOICEMODE_SESSION_NAME`, else
`~/.voicemode/session_names/$CLAUDE_CODE_SESSION_ID.txt`, else the repo name.

## Voice Barge-In (stop talking when interrupted)

Opt-in: the `voice_barge` MCP tool (`on`, `off`, `status`), or `voicemode-switch barge
on|off|status` from a shell. It stops TTS when the user talks over it and keeps the words
they said. Off takes effect at once, so use it whenever the user asks to turn interruption
on or off. The first turn of each session tells you its state once; say it to the user in a
few words, with how to switch it. See `docs/voice-barge-in/README.md`, including what has not
been tested.

When the user says "stop voice mode", call the `voice_stop` MCP tool and continue in text.

When the user asks to show or hide the voice pill, call the `voice_pill` MCP tool (show / hide / status).

When the user says voice mode ends the turn too late or too early, or background noise keeps it listening, call the `voice_sensitivity` MCP tool (status / set with `level` earlier, noisy, later or numbers / reset); it applies at the next listen, no restart. See `docs/voice-sensitivity/README.md`.

## Never End a Voice Conversation on Your Own Initiative

Only the **user** ends a voice conversation (e.g. "goodbye", "that's all",
"we're done", "stop voice mode"). After every exchange, keep the line open by
calling `mcp__voicemode__converse` again with `wait_for_response=true` — even
when you have nothing to add (then ask a short open question or just listen).
Finishing a thought, a natural pause, running out of things to say, or "the
burst is over" are NEVER reasons to stop. Do not wander off to write memory or
files in a way that drops the voice loop; if you must pause for longer work, say
so out loud and keep the conversation open.

## Available Voices (Kokoro TTS)

### American English
- Female: `af_sky` (default), `af_bella`, `af_heart`, `af_jessica`, `af_nicole`, `af_nova`, `af_sarah`, `af_alloy`
- Male: `am_adam`, `am_echo`, `am_eric`, `am_michael`, `am_liam`, `am_puck`, `am_fenrir`

### British English
- Female: `bf_alice`, `bf_emma`, `bf_lily`
- Male: `bm_daniel`, `bm_george`, `bm_lewis`

### Other Languages
- French: `ff_siwis` (F)
- Italian: `if_sara` (F), `im_nicola` (M)
- Spanish: `ef_dora` (F), `em_alex` (M)
- Hindi: `hf_alpha` (F), `hm_omega` (M)
- Japanese: `jf_alpha` (F), `jm_kumo` (M) — experimental, see below
- Portuguese: `pf_dora` (F), `pm_alex` (M)
- Chinese: `zf_xiaobei` (F), `zm_yunxi` (M) — experimental, see below

Japanese and Chinese are marked `"quality": "experimental"` by the Kokoro server and hidden in the voice pill unless named in `VOICEMODE_PILL_LANGUAGES`: espeak-ng alone phonemises them, and n=1 round trips on 2026-11-17 came back as nonsense (`docs/voice-pill/README.md`, "Languages and quality").

### German (Kokoro)
Kokoro handles German well with existing voices — just switch to German language in conversation.
No need to switch to Piper for German.

## Available Voices (Piper TTS)

### Piper TTS (multilingual, port 8881)
- German: `p_de_thorsten` (M, high quality), `p_de_eva` (F)
- Dutch: `p_nl_nathalie` (F)
- Polish: `p_pl_gosia` (F)
- Russian: `p_ru_dmitri` (M)
- Korean: `p_ko_hana` (F)

## Services

| Service | Port | Purpose |
|---------|------|---------|
| whisper-proxy | 2022 | Translates OpenAI-compatible STT to Whisper `/asr` |
| faster-whisper-server | 2022 | Native GPU STT, **instead of** whisper-proxy + Docker whisper when `STT_ENGINE=faster-whisper` in `~/.voicemode-local/config` (venv `FASTER_WHISPER_VENV`, model `FASTER_WHISPER_MODEL`, default small). Started via `scripts/faster-whisper-start.sh` (sets the CUDA library path first); `/health` is 503 until the model is loaded and warmed up. Binds `FASTER_WHISPER_HOST` (default 0.0.0.0, as whisper-proxy). WAV in-process, other formats via `ffmpeg` on PATH. Back to Docker: `STT_ENGINE=docker`. See `docs/native-gpu-stt/README.md`. |
| faster-whisper-server (CPU fallback) | 2023 | CPU twin of the GPU STT, routed right after 2022, when `STT_CPU_FALLBACK=true` (with `STT_ENGINE=faster-whisper`; port `STT_CPU_FALLBACK_PORT`, model `STT_CPU_FALLBACK_MODEL`, default base). Same venv and wrapper, `--device cpu --compute-type int8`; healthy only if `/health` says engine faster-whisper, device cpu. Measured 2026-11-17 (7.6 s clip, n=3): base 2.5-5.4 s at load ~5, 23-30 s at load ~25; 256 MiB idle. Opt-in: the installer asks, default No (`--cpu-fallback` = on). See `docs/native-gpu-stt/README.md` "CPU fallback". |
| voicemode-whisper | 9000 | Whisper ASR (Docker) |
| voicemode-kokoro | 8880 | Kokoro TTS (Docker mode, Kokoro-FastAPI) |
| kokoro-onnx-server | 8882 | Kokoro TTS (native ONNX). **482 MB measured** — 144 MB venv + 338 MB models. The old "~92MB" here was never measured and was wrong. Set `KOKORO_ONNX=true` + `KOKORO_ONNX_PORT=8882` in `~/.voicemode-local/config`; `ensure` starts it. `--no-download`: a missing model file exits 1 naming it and its URL (FELIX); an unknown voice is a 404 "Voice 'x' not found", no longer a bare 500. Runs ALONGSIDE the Docker Kokoro as the local last resort in the TTS chain — on 8880 it would replace it instead. With the GPU Kokoro on 8880, 8882 is instead its **CPU fallback** when `KOKORO_CPU_FALLBACK=true` (port `KOKORO_CPU_FALLBACK_PORT`): `scripts/kokoro-cpu-start.sh` runs it from the GPU venv on CPUExecutionProvider, routed after 8880 and 8881, with `VOICEMODE_TTS_TIMEOUT_LOCAL=90` written unless already set; healthy only if `/health` says CPUExecutionProvider; 453 MiB idle (measured 2026-11-17). Opt-in: the installer asks, default No (`--cpu-fallback` = on). See `docs/native-gpu-tts/README.md` "CPU fallback". |
| kokoro-onnx-server (GPU) | 8880 | Native GPU TTS, **instead of** the Docker Kokoro when `KOKORO_ONNX=true` + `KOKORO_ONNX_GPU=true` (+ `KOKORO_ONNX_PORT=8880`, the installer's choice with an NVIDIA GPU). Same server from its own venv (`KOKORO_ONNX_VENV`, default `~/.voicemode-local/kokoro-gpu-venv`, 3.3 GB measured) via `scripts/kokoro-gpu-start.sh` (CUDA library path + `ONNX_PROVIDER`) with `--require-gpu`: exits 1 unless the session is on CUDAExecutionProvider. Needs `onnxruntime-gpu==1.23.2` and the CPU `onnxruntime` removed (both silently run on the CPU otherwise). `/health` names the provider. `KOKORO_PROVIDER=auto|cuda|cpu|directml|coreml` in the config (installer asks auto/cpu/cuda with the GPU engine, default auto; `--kokoro-provider`, `VOICEMODE_KOKORO_PROVIDER`) passes `--provider` **instead of** `--require-gpu`: auto = CUDA only if the session really runs on it, else CPU with a log line and `/health` `fallback_reason`; explicit cuda/directml/coreml that cannot be had = exit 1. `KOKORO_THREADS=N` = `--threads` (intra-op; 0 = onnxruntime default). Unset: as before. DirectML/CoreML never run by us. Back to Docker: `KOKORO_ONNX_GPU=false`, `KOKORO_ONNX=false`. See `docs/native-gpu-tts/README.md` ("Provider and threads"). |
| piper-proxy | 8881 | OpenAI-compatible TTS via Piper |
| voicemode-piper | 10200 | Piper TTS (Docker, optional profile) |

## Mode Switching

Switch backends with the `voicemode-switch <mode>` CLI (writes `~/.voicemode/voicemode.env`); restart Claude Code afterward for changes to take effect. (Upstream voice-mode 8.7+ also exposes `update_config`/`config_reload` MCP tools that write the same file.) Our standalone `switch_mode` MCP tool/slash-command was retired in the 8.7.1 integration since upstream now does it natively.

Available modes: `local` (Kokoro+Piper equal, OpenAI last — recommended), `localonly` (no cloud), `piper` (Piper-primary), `openai` (cloud), `hybrid` (cloud STT + local TTS).

## Compute Mode (GPU vs CPU)

Orthogonal to the routing modes above: the **Docker** STT/TTS backends (Whisper +
Kokoro) run on CPU or the NVIDIA GPU. This is the lever for the "voice stalls / drops
mid-sentence under several sessions" failure — CPU-only Kokoro saturates the cores the
real-time audio pipeline needs. Three modes via `voicemode-switch compute [gpu|hybrid|cpu]`
(no arg = show current mode + GPU capability + image set):
- **`hybrid` (recommended w/ a GPU)** — Kokoro on GPU, Whisper on CPU. Moves the actual
  bottleneck (TTS) off-core; Whisper was never the bottleneck and its CUDA image is ~25GB,
  so it stays on the lean CPU image. Stacks `docker-compose.hybrid.yml`.
- **`gpu`** — both on GPU (best STT accuracy, big disk). Stacks `docker-compose.gpu.yml`.
- **`cpu`** — both on CPU; Kokoro capped at `KOKORO_CPUS` cores (default 6) so it can't
  starve playback.

Install auto-detects the GPU and defaults to hybrid. State in `~/.voicemode-local/config`
(`COMPUTE_MODE`, `WHISPER_MODEL`). The Whisper model is cached in the `whisper-cache`
volume so it isn't re-downloaded on every recreate. Switching recreates containers
(interrupts the active exchange) but does **not** change routing — no Claude Code restart.
GPU needs `nvidia-container-toolkit` + the `nvidia` docker runtime; the Docker compose
files are the same on Linux / WSL / Windows. See `docs/compute-modes/README.md`.

## STT Timeout (and the ~61s stall)

Upstream hardcodes a 60s read timeout on the STT client. When a local
transcription hangs, that 60s **is** the whole cost: the retry then succeeds in
about a second, so nothing errors — the turn just goes silent for ~61.5s.
Measured 2026-09-06: 22% of calls that day, and zero in the 5,651 before it.

`patches/patch_simple_failover.py` makes the timeout configurable. Defaults are
unchanged, so it is a no-op until you opt in:

| var | scope | default |
|---|---|---|
| `VOICEMODE_STT_TIMEOUT` | all STT endpoints | 60 |
| `VOICEMODE_STT_TIMEOUT_LOCAL` | local endpoints only | as above |

This machine sets `VOICEMODE_STT_TIMEOUT_LOCAL=60`, because `hybrid` runs Whisper
on the CPU: a 5.5s clip took about 29s under load (2026-11-04), so the earlier 15
failed and retried. **Size it by measurement, not feel — local STT time scales with
audio length**, and compute mode changes the answer: GPU whisper `small` here is
median 1.1s / p95 3.6s / max 6.6s (n=67), which is what 15 was sized on. Takes effect on a fresh voice-mode process (`/mcp` → reconnect;
no full Claude Code restart needed).

The same patch makes upstream's hardcoded **30s TTS timeout** configurable
(`VOICEMODE_TTS_TIMEOUT`, `VOICEMODE_TTS_TIMEOUT_LOCAL`; defaults unchanged). A loaded
local Kokoro can need longer for one long sentence (Windows 2026-11-13, n=1: 12.4s of
audio in 40.9s), and with streaming off while barge-in may listen, the whole sentence
must finish inside that timeout.

**Long replies (2026-11-14).** The local Kokoro server answers only when the whole audio is
ready, so a long reply must be generated inside one timeout. On a Windows laptop 2.5-5x slower
than real time, 1,178 characters (54 s of audio) took 134.7 s and timed out at 90 s; upstream
then sent the same text again buffered, so the server generated it twice and the turn waited
180 s for nothing. Three changes:
- `VOICEMODE_TTS_CHUNK_CHARS` (default 300 since 2026-11-17; 0 = off; an invalid value falls
  back to 300 and is logged once): speak a reply in parts of at most that many
  characters, split at sentence ends, one request each (`patches/tts_chunks.py`). A stop,
  hold or barge-in ends the reply, also while the next part is generated; a failed part ends
  it and its error says which part. Size it with `VOICEMODE_TTS_TIMEOUT_LOCAL` (the default
  timeout is 30 s); limits are listed in the module docstring.
- A streaming request to a local server that timed out is not sent again buffered; the error
  says so (`patches/patch_tts_no_double_synthesis.py`). Other failures still fall back.
- `kokoro-onnx-server.py` serves each request on its own thread, so `/health` answers during a
  long synthesis; speech is still generated one request at a time.
Parts are generated one after another, so a machine slower than real time pauses between them.
Default 300 approved by the owner 2026-11-17, from a measurement (load 23-53, n=2-3, indicative)
of time to first audio for a 980-character text: whole text GPU 15.9-19.4 s, CPU 157 s;
500-character parts 7-10 s GPU / 31 s CPU; 300-character parts 3.3-3.8 s GPU / 38 s CPU;
150-character parts 2.4-3.1 s GPU / 12 s CPU. On GPU each 300-character part generated 3-5x
faster than it plays (no pauses); on CPU every part is slower than playback (gaps of about
0.4-20 s). A reply of at most 300 characters is still one request, with no extra log line.

Root cause is still open — see `docs/stt-61s-stall/README.md` for what has
already been ruled out by measurement, so it doesn't get re-proposed.

**Mic stream fails to start** ("Wait timed out [PaErrorCode -9987]", 2026-11-18): `patches/patch_audio_recover.py` re-initialises PortAudio and retries once with silence detection (behaviour change: old behaviour via `VOICEMODE_AUDIO_RECOVER=false`); the fixed-length fallback ends on ESC/cancel and `VOICEMODE_FALLBACK_RECORD_MAX_S` caps it (unset = unchanged). See `docs/audio-recover/README.md`.

**Routing config lives in `~/.voicemode/voicemode.env`** (plural `VOICEMODE_TTS_BASE_URLS`/`VOICEMODE_STT_BASE_URLS`/`VOICEMODE_VOICES`), NOT `~/.claude.json` (which holds only `OPENAI_API_KEY`). voice-mode reads ONLY the plural list vars; the singular `TTS_BASE_URL`/etc. are ignored. Each requested voice routes to the first engine that owns it (Kokoro and Piper reject foreign voices fast); OpenAI is strictly last-resort and is **never** silently substituted for a local voice. The MCP command is the `voicemode-mcp` wrapper, which auto-starts (detached) the needed proxies on session load. Test routing with `voicemode-switch test-tts`.

## Whisper Proxy Auto-Start

The whisper proxy starts automatically on first WSL shell via `~/.bashrc`. It checks port 2022 to avoid duplicates.

With `STT_ENGINE=faster-whisper` in `~/.voicemode-local/config` that autostart no longer takes
port 2022: `whisper-proxy.py` reads the config, prints `STT_ENGINE=faster-whisper: not starting the
Docker proxy; run voicemode-switch ensure` to stderr and exits 0 (`--force` serves anyway). Its
`/health` reports the backend: 200 only if the Docker whisper answers, else 503
`backend_unreachable`. `voicemode-switch ensure` treats 2022 as healthy only if `/health` says
`"engine": "faster-whisper"` (and 8880 only with provider CUDAExecutionProvider when
`KOKORO_ONNX_GPU=true`); see `docs/native-gpu-stt/README.md`.

## Key Dependencies

- Python 3.10+, Docker + Compose, Claude Code CLI, uv/uvx
- For Piper native mode: `piper-tts` (pip)
- For cloud modes: OpenAI API key
