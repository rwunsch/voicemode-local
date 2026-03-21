# VoiceMode Local Setup

Local voice services for [VoiceMode MCP](https://github.com/mbailey/voicemode) on WSL2/Windows 11, enabling two-way voice conversations with Claude Code — fully local or via OpenAI.

## Quick Start

```bash
# Clone and install
cd ~/git/voicemode-local
./install.sh --openai-key=sk-proj-...   # OpenAI key is optional (only for hybrid/openai modes)

# Switch to fully local mode (free, private)
voicemode-switch local

# Restart Claude Code, then:
#   /voicemode:converse
```

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Claude Code + VoiceMode MCP                                 │
│    STT_BASE_URL → http://127.0.0.1:2022/v1  (local mode)    │
│    TTS_BASE_URL → http://127.0.0.1:8880/v1  (local mode)    │
└──────┬───────────────────────────────┬───────────────────────┘
       │                               │
       ▼                               ▼
┌──────────────────┐          ┌──────────────────┐
│  whisper-proxy   │          │  Kokoro-FastAPI   │
│  :2022           │          │  :8880            │
│  (Python)        │          │  (Docker)         │
│                  │          │  OpenAI-compat    │
│  Translates:     │          │  /v1/audio/speech │
│  /v1/audio/      │          └──────────────────┘
│  transcriptions  │
│       │          │
│       ▼          │
│  faster-whisper  │
│  :9000 (Docker)  │
│  /asr endpoint   │
└──────────────────┘
```

### Why the proxy?

VoiceMode expects OpenAI-compatible endpoints (`/v1/audio/transcriptions`). The best
Docker-based Whisper service ([onerahmet/openai-whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice))
uses a different endpoint (`/asr`). The official whisper.cpp Docker image has a known bug
where it crashes on multipart form uploads in Docker/WSL2. The proxy bridges this gap.

## Modes

| Mode | STT | TTS | Cost | Quality |
|------|-----|-----|------|---------|
| `local` | Local Whisper (via proxy) | Local Kokoro | Free | Good |
| `openai` | OpenAI Whisper API | OpenAI TTS | ~$0.01/min | Best |
| `hybrid` | OpenAI Whisper API | Local Kokoro | ~$0.006/min | Good TTS, best STT |

## Commands

```bash
voicemode-switch local    # Fully local (free, private)
voicemode-switch openai   # Fully cloud (best quality)
voicemode-switch hybrid   # Local TTS + cloud STT (balanced)
voicemode-switch status   # Check services and current config
voicemode-switch start    # Start Docker containers + proxy
voicemode-switch stop     # Stop everything
```

After switching modes, **restart Claude Code** for changes to take effect.

## What the installer does

The `install.sh` script configures the following locations:

### Files created by this project

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Whisper + Kokoro Docker service definitions |
| `whisper-proxy.py` | Translates OpenAI `/v1/audio/transcriptions` → Whisper `/asr` |
| `voicemode-switch` | CLI to switch modes and manage services |
| `install.sh` | One-time setup script |
| `models/` | Local model files (gitignored) |

### System files modified (outside this repo)

| File | What's added | Purpose |
|------|-------------|---------|
| `~/.asoundrc` | ALSA→PulseAudio PCM/CTL config | Routes audio through WSLg |
| `~/.claude.json` | `mcpServers.voicemode` entry | Registers VoiceMode MCP + env vars (STT/TTS URLs) |
| `~/.claude/settings.json` | `permissions.allow` entries | Auto-allows `converse` and `service` tools |
| `~/.bashrc` | `export OPENAI_API_KEY=...` | Makes key available in shell (optional) |
| `~/.local/bin/voicemode-switch` | Symlink → this repo | Puts `voicemode-switch` in PATH |
| `/tmp/whisper-proxy.pid` | PID file | Tracks running proxy process |
| `/tmp/whisper-proxy.log` | Log file | Proxy stdout/stderr |

### Docker containers

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| `voicemode-whisper` | `onerahmet/openai-whisper-asr-webservice` | 9000 | Speech-to-text (faster-whisper) |
| `voicemode-kokoro` | `ghcr.io/remsky/kokoro-fastapi-cpu` | 8880 | Text-to-speech (Kokoro-82M) |

## Prerequisites

- **WSL2** on Windows 11 with WSLg (for audio passthrough)
- **Docker** installed and running in WSL
- **Claude Code** CLI installed (`claude` command available)
- **uv/uvx** installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Python 3.10+**
- **OpenAI API key** (only for `openai` and `hybrid` modes)

### Required apt packages (installed automatically)

`libasound2-plugins libasound2-dev libportaudio2 portaudio19-dev alsa-utils sox python3-dev`

## Uninstall

```bash
# Stop services
voicemode-switch stop

# Remove symlink
rm ~/.local/bin/voicemode-switch

# Remove MCP registration (from Claude Code)
claude mcp remove voicemode

# Remove ALSA config (if you don't need it for other things)
rm ~/.asoundrc

# Remove OpenAI key from bashrc (edit manually)
# Remove permissions from ~/.claude/settings.json (edit manually)

# Remove this repo
rm -rf ~/git/voicemode-local
```

## Troubleshooting

### No audio output after switching Windows device
WSLg routes audio through the Windows default device. When you switch devices:
```bash
pactl suspend-sink RDPSink 1 && pactl suspend-sink RDPSink 0
```
Or restart WSL entirely from PowerShell: `wsl --shutdown`

### Proxy not starting
```bash
cat /tmp/whisper-proxy.log
lsof -i:2022   # check if port is in use
```

### Whisper container not ready
The model downloads on first start (~300MB). Check progress:
```bash
docker logs voicemode-whisper
```

### Microphone not working
```bash
# Check PulseAudio
pactl info
pactl list sources short

# Test recording
arecord -D default -f S16_LE -r 44100 -c 1 -d 1 /tmp/test.wav

# Windows: Settings → Privacy → Microphone → Allow desktop apps
```

### VoiceMode not connecting
```bash
# Check MCP server status
claude mcp list | grep voicemode

# Check env vars are set
python3 -c "
import json, os
with open(os.path.expanduser('~/.claude.json')) as f:
    d = json.load(f)
print(json.dumps(d['mcpServers']['voicemode']['env'], indent=2))
"
```
