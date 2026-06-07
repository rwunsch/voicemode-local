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

## Switching Voices Mid-Conversation

If the user asks to change voice during a conversation, switch the `voice` parameter on the next `mcp__voicemode__converse` call. Acknowledge the switch briefly.

## Session Queue (multiple concurrent voice sessions)

Concurrent Claude Code sessions take strict FIFO turns on the voice channel.
Two non-negotiable rules when using `mcp__voicemode__converse`:

1. **QUEUED status**: if converse returns "QUEUED — position N of M ...",
   immediately call converse again with the same message and the given
   `ticket` value. Repeat as long as needed. Never degrade the question to
   text; never give up.
2. **`end_burst=true`** on your final exchange of a conversation burst, so the
   next waiting session gets the floor. Forgetting costs ~90s (auto-release).

Inspect the queue anytime with `./voicemode-switch queue`.

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

## In-Session Mode Switching

Use `/voicemode:switch-mode` to switch between TTS engines (e.g. Kokoro <-> Piper) without leaving Claude Code. Restart Claude Code afterward for changes to take effect.

Available modes: `local` (Kokoro), `piper`, `openai`, `hybrid`.

## Whisper Proxy Auto-Start

The whisper proxy starts automatically on first WSL shell via `~/.bashrc`. It checks port 2022 to avoid duplicates.

## Key Dependencies

- Python 3.10+, Docker + Compose, Claude Code CLI, uv/uvx
- For Piper native mode: `piper-tts` (pip)
- For cloud modes: OpenAI API key
