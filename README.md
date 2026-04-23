# VoiceMode Local

Talk to Claude Code with your voice — fully local, private, and free.

VoiceMode Local sets up local speech-to-text (Whisper) and text-to-speech (Kokoro, Piper) services so you can have real-time two-way voice conversations with Claude Code without sending audio to the cloud. It extends the [VoiceMode MCP](https://github.com/mbailey/voicemode) server with local voice infrastructure and multi-engine support.

## How It Works

1. **You speak** — hold Space to record, release to send
2. **Silence detection** stops recording automatically when you pause
3. **A chime plays** to confirm your input was captured
4. **Whisper transcribes** your speech locally (no cloud)
5. **Claude responds in voice** — you hear the reply through Kokoro or Piper TTS
6. **Another chime** signals it's your turn again

The conversation flows naturally. Claude's responses are typically 1-3 seconds to start speaking, and the full round-trip (speak, transcribe, think, synthesize, play) takes 10-20 seconds depending on response length.

## Why Use This?

**Privacy** — Your voice never leaves your machine. STT and TTS run locally via Docker containers.

**Cost** — Fully local mode is free. No API charges for voice, no per-minute billing.

**Multi-session voice** — Run multiple Claude Code sessions and give each a different voice. When three terminals are talking to you, you can tell which is which by the voice alone. This is the killer feature for power users running parallel Claude Code sessions.

**Language switching** — Say "sprich Deutsch" and Claude switches to German mid-conversation. Whisper auto-detects your language, no manual configuration needed. Switch back with "speak English". The TTS voices handle multiple languages naturally.

**Engine switching** — Swap between Kokoro (fast, good English), Piper (multilingual), or OpenAI (best quality) on the fly using `/voicemode:switch-mode` without leaving your session.

## What VoiceMode Local Adds

The upstream [VoiceMode MCP](https://github.com/mbailey/voicemode) provides the core voice conversation framework for Claude Code. VoiceMode Local builds on it with local infrastructure:

| Feature | VoiceMode (upstream) | VoiceMode Local |
|---------|---------------------|-----------------|
| STT | OpenAI Whisper API (cloud) | Local Whisper via Docker (free) |
| TTS | OpenAI TTS (cloud) | Kokoro + Piper locally, OpenAI as fallback |
| Cost | ~$0.01/min | Free (local modes) |
| Privacy | Audio sent to OpenAI | Audio stays on your machine |
| Voice engines | OpenAI only | Kokoro, Piper, OpenAI — switchable |
| Voice selection | Fixed | Random or named voice per session |
| Language switching | Via OpenAI | Automatic (Whisper detects language) |
| Multi-session | Same voice everywhere | Different voice per session |
| Mode switching | N/A | `/voicemode:switch-mode` in-session |

## Supported Environments

| Environment | Status | Notes |
|-------------|--------|-------|
| **WSL2 on Windows 11** | Tested | Primary target. Requires WSLg for audio passthrough |
| **Ubuntu 22.04+** | Should work | PulseAudio or PipeWire required for audio. Skip `~/.asoundrc` step |
| **Fedora / RHEL** | Should work | Same as Ubuntu; use `dnf` instead of `apt` for system packages |
| **macOS** | Untested | Docker services should work. Audio uses CoreAudio, not ALSA — skip `~/.asoundrc`. Microphone access may need System Settings approval |
| **NixOS** | Untested | Docker works. System packages need a Nix derivation instead of `apt install` |
| **Windows (native)** | Not supported | Use WSL2 — our proxies assume Unix and ALSA doesn't exist on Windows |

**Tested on:** Ubuntu 22.04 LTS under WSL2 (kernel 6.6.x), Windows 11 24H2.

## Dependencies

| Dependency | Required | Purpose | Install |
|------------|----------|---------|---------|
| **Python 3.10+** | Yes | Runs proxies and patches | Pre-installed on Ubuntu 22.04+ |
| **Docker + Docker Compose** | For Docker mode | Runs Whisper, Kokoro, Piper containers | [docs.docker.com](https://docs.docker.com/engine/install/) |
| **Claude Code** | Yes | `claude` CLI must be in PATH | `npm install -g @anthropic-ai/claude-code` |
| **uv/uvx** | Yes | Runs VoiceMode MCP server | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **piper-tts** | For native Piper | Piper TTS CLI (auto-installed in native mode) | `pip install piper-tts` |
| **OpenAI API key** | For openai/hybrid modes | Cloud STT/TTS | [platform.openai.com](https://platform.openai.com/api-keys) |

### System packages (installed automatically by `install.sh`)

```
libasound2-plugins libasound2-dev libportaudio2 portaudio19-dev alsa-utils sox python3-dev
```

### Upstream Projects

This project patches and orchestrates the following open-source projects:

| Project | Role | License | Repository |
|---------|------|---------|------------|
| **VoiceMode** | MCP server for voice conversations | MIT | [mbailey/voicemode](https://github.com/mbailey/voicemode) |
| **Kokoro-FastAPI** | Local TTS (Kokoro-82M model) | Apache-2.0 | [remsky/Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI) |
| **Piper** | Local TTS (multilingual) | MIT | [rhasspy/piper](https://github.com/rhasspy/piper) |
| **Wyoming Piper** | Piper Docker container | MIT | [rhasspy/wyoming-piper](https://github.com/rhasspy/wyoming-piper) |
| **Whisper ASR Webservice** | Local STT (faster-whisper engine) | MIT | [ahmetoner/whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice) |
| **faster-whisper** | Whisper inference engine (inside Docker) | MIT | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) |

VoiceMode Local applies patches to the installed VoiceMode package to add voice selection, Piper routing, and mode switching. These patches need to be re-applied after upgrading VoiceMode (`./patches/apply.sh`).

## How To Use

### 1. Install

```bash
git clone <this-repo> ~/git/voicemode-local
cd ~/git/voicemode-local
./install.sh
```

The installer will:
- Ask you to choose **Docker** or **native** install mode
- Ask if you want **Piper TTS** for multilingual voices (German, Dutch, etc.)
- Install system packages, configure ALSA, register the VoiceMode MCP server
- Start Docker containers (in Docker mode) and proxy services
- Apply patches to extend VoiceMode with voice selection and mode switching

Optionally pass your OpenAI key (only needed for openai/hybrid modes):
```bash
./install.sh --openai-key=sk-proj-...
```

### OpenAI API Key

The OpenAI key is only needed for `openai` and `hybrid` modes (cloud STT/TTS). It's stored in two places:

1. **`~/.claude.json`** (under `mcpServers.voicemode.env.OPENAI_API_KEY`) — The VoiceMode MCP server reads it here at startup to authenticate with OpenAI's STT/TTS APIs. This is updated automatically by the installer and `voicemode-switch`.

2. **`~/.bashrc`** (as `export OPENAI_API_KEY=...`) — Makes the key available in your shell for other tools that need it. This is optional and only added if you provide a key during install.

**Why two places?** The MCP server runs in its own process and reads env vars from `~/.claude.json`, not from your shell. Your shell reads `~/.bashrc`. They don't share an environment, so the key needs to be in both.

To set or update the key after install:
```bash
# Option 1: Re-run the installer
./install.sh --openai-key=sk-proj-YOUR-KEY

# Option 2: Set manually
export OPENAI_API_KEY="sk-proj-YOUR-KEY"
echo 'export OPENAI_API_KEY="sk-proj-YOUR-KEY"' >> ~/.bashrc
voicemode-switch openai   # This writes the key into ~/.claude.json
```

> **Security note:** Storing API keys directly in `~/.bashrc` is convenient but not ideal — the key is visible in plain text. A more secure approach is to keep keys in a separate file with restricted permissions and source it:
> ```bash
> echo 'OPENAI_API_KEY="sk-proj-YOUR-KEY"' > ~/.config/voicemode-local/secrets
> chmod 600 ~/.config/voicemode-local/secrets
> echo 'source ~/.config/voicemode-local/secrets' >> ~/.bashrc
> ```
> Note: `~/.claude.json` always requires the actual key value (no variable references) — this is a Claude Code limitation.

### 2. Choose a mode

```bash
voicemode-switch local    # Kokoro TTS + local Whisper (free, private)
voicemode-switch piper    # Piper TTS + local Whisper (free, multilingual)
voicemode-switch openai   # OpenAI cloud TTS + STT (best quality, ~$0.01/min)
voicemode-switch hybrid   # Local Kokoro TTS + cloud STT (~$0.006/min)
```

### 3. Restart Claude Code and start talking

```bash
claude
# Then in Claude Code, type:
#   /voicemode:converse
```

Claude will offer you a voice selection, then start a two-way voice conversation.

### Switching modes mid-session

From within Claude Code, use `/voicemode:switch-mode` to change between engines without leaving the session. Claude Code needs a restart afterward for the new settings to take effect.

### Managing services

```bash
voicemode-switch start    # Start Docker containers + proxies
voicemode-switch stop     # Stop everything
voicemode-switch status   # Health check all services
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Claude Code + VoiceMode MCP                                         │
│    STT_BASE_URL → http://127.0.0.1:2022/v1  (local mode)            │
│    TTS_BASE_URL → http://127.0.0.1:8880/v1  (kokoro/local mode)     │
│    TTS_BASE_URL → http://127.0.0.1:8881/v1  (piper mode)            │
└──────┬────────────────────────────┬──────────────────┬───────────────┘
       │                            │                  │
       ▼                            ▼                  ▼
┌──────────────────┐       ┌──────────────────┐  ┌──────────────────┐
│  whisper-proxy   │       │  Kokoro-FastAPI   │  │  piper-proxy     │
│  :2022           │       │  :8880            │  │  :8881           │
│  (Python)        │       │  (Docker)         │  │  (Python)        │
│                  │       │  OpenAI-compat    │  │  OpenAI-compat   │
│  Translates:     │       │  /v1/audio/speech │  │  /v1/audio/speech│
│  /v1/audio/      │       └──────────────────┘  │       │          │
│  transcriptions  │                              │       ▼          │
│       │          │                              │  piper-tts CLI   │
│       ▼          │                              │  (local binary)  │
│  faster-whisper  │                              └──────────────────┘
│  :9000 (Docker)  │
│  /asr endpoint   │
└──────────────────┘
```

### Why the proxies?

**Whisper proxy** — VoiceMode expects OpenAI-compatible endpoints (`/v1/audio/transcriptions`). The best Docker-based Whisper service ([onerahmet/openai-whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice)) uses a different endpoint (`/asr`). The proxy bridges this gap and filters out the `language=auto` parameter that causes the backend to return 500.

**Piper proxy** — Piper TTS is a CLI tool, not an HTTP server. The proxy wraps it in an OpenAI-compatible API so VoiceMode can use it just like Kokoro or OpenAI TTS. Models are auto-downloaded from HuggingFace on first use.

## Modes

| Mode | STT | TTS | Cost | Best for |
|------|-----|-----|------|----------|
| `local` | Local Whisper (via proxy) | Local Kokoro | Free | English, French, Italian, Spanish, Japanese |
| `piper` | Local Whisper (via proxy) | Local Piper | Free | German, Dutch, Polish, Russian, Korean |
| `openai` | OpenAI Whisper API | OpenAI TTS | ~$0.01/min | Best quality, any language |
| `hybrid` | OpenAI Whisper API | Local Kokoro | ~$0.006/min | Best STT accuracy + free TTS |

## Available Voices

### Kokoro TTS (local, port 8880)

| Language | Female | Male |
|----------|--------|------|
| American English | af_sky (default), af_bella, af_heart, af_jessica, af_nicole, af_nova, af_sarah, af_alloy | am_adam, am_echo, am_eric, am_michael, am_liam, am_puck, am_fenrir |
| British English | bf_alice, bf_emma, bf_lily | bm_daniel, bm_george, bm_lewis |
| French | ff_siwis | |
| Italian | if_sara | im_nicola |
| Spanish | ef_dora | em_alex |
| Hindi | hf_alpha | hm_omega |
| Japanese | jf_alpha | jm_kumo |
| Portuguese | pf_dora | pm_alex |
| Chinese | zf_xiaobei | zm_yunxi |

### Piper TTS (local, port 8881)

| Language | Voice | Gender | Quality |
|----------|-------|--------|---------|
| German | p_de_thorsten | Male | High |
| German | p_de_eva | Female | Medium |
| Dutch | p_nl_nathalie | Female | Medium |
| Polish | p_pl_gosia | Female | Medium |
| Russian | p_ru_dmitri | Male | Medium |
| Korean | p_ko_hana | Female | Medium |

### OpenAI TTS (cloud)

alloy (default), echo, fable, nova, onyx, shimmer

## Services

| Service | Port | Type | Purpose |
|---------|------|------|---------|
| whisper-proxy | 2022 | Python process | Translates OpenAI STT API → Whisper `/asr` |
| voicemode-whisper | 9000 | Docker container | Whisper ASR (faster-whisper engine) |
| voicemode-kokoro | 8880 | Docker container | Kokoro TTS (Docker mode, Kokoro-FastAPI) |
| kokoro-onnx-server | 8880 | Python process | Kokoro TTS (native mode, lightweight ONNX) |
| piper-proxy | 8881 | Python process | OpenAI-compatible TTS via piper-tts CLI |
| voicemode-piper | 10200 | Docker container | Piper TTS (optional, profile: piper) |

**Kokoro TTS options:** Docker mode uses Kokoro-FastAPI (multi-GB image, fast inference). Native mode uses kokoro-onnx (~92MB model, CPU-only, no Docker needed). Both serve the same OpenAI-compatible API on port 8880 with identical voice quality.

## Patches

VoiceMode Local extends the upstream VoiceMode MCP server through patches applied to the installed package. These add:

- **Voice selection** — on conversation start, Claude offers to pick a random voice or let you choose
- **Voice routing** — voices are routed to the correct engine by prefix (`af_` → Kokoro, `p_` → Piper, `alloy` → OpenAI)
- **Language fallback** — if a requested language isn't available in the current engine, Claude suggests switching
- **Mode switching** — `/voicemode:switch-mode` lets you change engines from within Claude Code

Patches are applied by `./patches/apply.sh` (run automatically during install) and need to be re-applied after upgrading the VoiceMode package.

## Project Files

| File | Purpose |
|------|---------|
| `install.sh` | One-time setup (Docker or native mode, Piper opt-in) |
| `docker-compose.yml` | Whisper + Kokoro + Piper (optional) container definitions |
| `whisper-proxy.py` | OpenAI `/v1/audio/transcriptions` → Whisper `/asr` translator |
| `piper-proxy.py` | OpenAI `/v1/audio/speech` → piper-tts CLI wrapper |
| `voicemode-switch` | CLI for mode switching and service management |
| `voices/piper-voices.json` | Curated Piper voice catalog with model metadata |
| `patches/converse.py` | Extended conversation prompt (voice selection, routing, fallback) |
| `patches/switch_mode.py` | MCP tool for in-session mode switching |
| `patches/switch_mode_prompt.py` | MCP prompt for `/voicemode:switch-mode` slash menu entry |
| `patches/apply.sh` | Copies patches into the installed voice_mode package |
| `tests/` | Test suite (42 tests: proxies, voice catalog, mode switching) |

## System Files Modified

The installer creates or modifies these files outside the repo:

**`~/.asoundrc`** — ALSA audio routing (created if missing)
```
pcm.!default { type pulse }
ctl.!default { type pulse }
```
Routes audio through PulseAudio/WSLg so microphone and speakers work in WSL2.

**`~/.claude.json`** — MCP server registration (key `mcpServers.voicemode`)
```json
{
  "mcpServers": {
    "voicemode": {
      "command": "/path/to/voicemode-local/.venv/bin/voice-mode",
      "args": [],
      "env": {
        "STT_BASE_URL": "http://127.0.0.1:2022/v1",
        "TTS_BASE_URL": "http://127.0.0.1:8880/v1",
        "TTS_VOICE": "af_sky",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```
The `env` block is updated by `voicemode-switch` when you change modes. The `OPENAI_API_KEY` is only needed for openai/hybrid modes.

**`~/.claude/settings.json`** — Permission allow-list (key `permissions.allow`)
Adds `mcp__voicemode__converse`, `mcp__voicemode__service`, and `mcp__voicemode__switch_mode` so Claude Code doesn't prompt for permission on every voice call.

**`~/.bashrc`** — Shell environment (optional)
Adds `export OPENAI_API_KEY="sk-..."` if you provided a key during install. Only needed for openai/hybrid modes.

**`~/.local/bin/voicemode-switch`** — Symlink to `voicemode-switch` in this repo, so the command is available in PATH.

**`~/.voicemode-local/config`** — Install preferences
```
INSTALL_MODE=docker
PIPER_ENABLED=true
```
Read by `voicemode-switch` to know whether to include the Piper Docker profile.

## Uninstall

```bash
voicemode-switch stop
rm ~/.local/bin/voicemode-switch
claude mcp remove voicemode
rm ~/.asoundrc                    # if not needed for other things
rm -rf ~/.voicemode-local
rm -rf ~/git/voicemode-local
# Manually remove OPENAI_API_KEY from ~/.bashrc if added
# Manually remove permissions from ~/.claude/settings.json
```

## Troubleshooting

### No audio output after switching Windows device
WSLg routes audio through the Windows default device. When you switch:
```bash
pactl suspend-sink RDPSink 1 && pactl suspend-sink RDPSink 0
```
Or restart WSL from PowerShell: `wsl --shutdown`

### Proxy not starting
```bash
cat /tmp/whisper-proxy.log        # or /tmp/piper-proxy.log
lsof -i:2022                     # check if port is in use
lsof -i:8881                     # check piper proxy port
```

### Whisper container not ready
The model downloads on first start (~300MB):
```bash
docker logs voicemode-whisper
```

### Microphone not working
```bash
pactl info                        # check PulseAudio
pactl list sources short          # list audio sources
arecord -D default -f S16_LE -r 44100 -c 1 -d 1 /tmp/test.wav  # test recording
# Windows: Settings → Privacy → Microphone → Allow desktop apps
```

### STT silently falling back to OpenAI
VoiceMode tries each STT endpoint in order and falls back silently on errors:
```bash
curl -s http://127.0.0.1:2022/health               # proxy running?
curl -s http://127.0.0.1:2022/v1/models             # responds to discovery?
curl -s http://127.0.0.1:9000/docs > /dev/null && echo OK  # backend reachable?
```
**Common cause**: VoiceMode sends `language=auto` for local providers, but the Whisper ASR backend returns HTTP 500. The whisper-proxy filters this automatically.

### VoiceMode not connecting
```bash
claude mcp list | grep voicemode
python3 -c "
import json, os
with open(os.path.expanduser('~/.claude.json')) as f:
    d = json.load(f)
print(json.dumps(d['mcpServers']['voicemode']['env'], indent=2))
"
```

### Piper voice downloads failing
Piper models are downloaded from HuggingFace on first use. If downloads fail:
```bash
# Check proxy logs
cat /tmp/piper-proxy.log

# Test download manually
curl -L -o /tmp/test.onnx \
  "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx"
```
