# VoiceMode Local

Local voice services for Claude Code, with Whisper STT, Kokoro TTS, and Piper TTS.

## Supported Environments

- **WSL2 on Windows 11** (primary, tested on Ubuntu 22.04 LTS)
- **Ubuntu 22.04+** (native Linux, should work with PulseAudio/PipeWire)
- **macOS** (untested — Docker services should work, audio routing may differ)
- **Windows (native)** — work in progress, see below

## Windows Working Copy

A second checkout lives at `C:\git\voicemode-local` (from WSL: `/mnt/c/git/voicemode-local`) holding the Windows port work on branch `feature/piper-integration` (commit `c03c099`, pushed but not merged to main). It adds `install.ps1` (modes: `wsl-mcp` / `wsl-shared` / `windows-native`), `voicemode-services.ps1`, Windows stdlib shims (`patches/fcntl_shim.py`, `patches/resource_shim.py`), and `docs/windows-issues.md` — a catalogue of 6 upstream voice-mode/Claude Code issues blocking native Windows. The `wsl-mcp` bridge mode works; true native mode is blocked mainly by issue #5 (recording loop hangs when sounddevice callbacks starve). When editing files in that copy, expect CRLF line endings.

## Voice Selection on Startup

When starting a voice conversation via `/voicemode:converse`, offer voice selection before the first message:

> "Starting voice mode. Want me to pick a random voice for this session, or use the default (af_sky)? You can also name a specific voice."

- **Random voice**: Pick from this pool of distinct-sounding voices (excludes current default):
  `af_bella`, `af_heart`, `af_nova`, `am_adam`, `am_eric`, `am_puck`, `bf_emma`, `bm_daniel`, `bm_george`, `ff_siwis`, `if_sara`, `im_nicola`
- **Named voice**: Use it directly as the `voice` parameter
- **Default / "just start"**: Proceed without specifying a voice

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
   default) blocks until the floor is granted to you. `"callback"` returns
   immediately and pings you when your turn comes — your message is **not**
   spoken at call time; you take the floor by calling converse again when
   notified. Never downgrade a queued question to text, and never give up.
3. **`hold_conch=true`** keeps the floor *across turns* — use it when your next
   converse call continues the same thread (asking a question you will answer,
   or speaking across several turns) so another agent cannot cut in mid-thought.
   It is a short, refreshed TTL (10s idle, re-stamped each turn); override per
   call with `conch_hold_timeout`. Release by simply not passing it next turn.

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
- Japanese: `jf_alpha` (F), `jm_kumo` (M)
- Portuguese: `pf_dora` (F), `pm_alex` (M)
- Chinese: `zf_xiaobei` (F), `zm_yunxi` (M)

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
| voicemode-whisper | 9000 | Whisper ASR (Docker) |
| voicemode-kokoro | 8880 | Kokoro TTS (Docker mode, Kokoro-FastAPI) |
| kokoro-onnx-server | 8880 | Kokoro TTS (native mode, lightweight ONNX ~92MB) |
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

**Routing config lives in `~/.voicemode/voicemode.env`** (plural `VOICEMODE_TTS_BASE_URLS`/`VOICEMODE_STT_BASE_URLS`/`VOICEMODE_VOICES`), NOT `~/.claude.json` (which holds only `OPENAI_API_KEY`). voice-mode reads ONLY the plural list vars; the singular `TTS_BASE_URL`/etc. are ignored. Each requested voice routes to the first engine that owns it (Kokoro and Piper reject foreign voices fast); OpenAI is strictly last-resort and is **never** silently substituted for a local voice. The MCP command is the `voicemode-mcp` wrapper, which auto-starts (detached) the needed proxies on session load. Test routing with `voicemode-switch test-tts`.

## Whisper Proxy Auto-Start

The whisper proxy starts automatically on first WSL shell via `~/.bashrc`. It checks port 2022 to avoid duplicates.

## Key Dependencies

- Python 3.10+, Docker + Compose, Claude Code CLI, uv/uvx
- For Piper native mode: `piper-tts` (pip)
- For cloud modes: OpenAI API key
