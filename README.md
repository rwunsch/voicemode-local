# VoiceMode Local

Local voice services for [VoiceMode MCP](https://github.com/mbailey/voicemode) — enabling two-way voice conversations with Claude Code using fully local STT and TTS, with optional cloud fallback.

## Supported Environments

| Environment | Status | Notes |
|-------------|--------|-------|
| **WSL2 on Windows 11** | Tested | Primary target. Requires WSLg for audio passthrough |
| **Ubuntu 22.04+ (native)** | Should work | PulseAudio or PipeWire required for audio |
| **macOS** | Untested | Docker services should work; audio routing may differ |
| **Windows (native)** | Not supported | Use WSL2 instead |

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
| voicemode-kokoro | 8880 | Docker container | Kokoro TTS (82M parameter model) |
| piper-proxy | 8881 | Python process | OpenAI-compatible TTS via piper-tts CLI |
| voicemode-piper | 10200 | Docker container | Piper TTS (optional, profile: piper) |

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

| File | What | Purpose |
|------|------|---------|
| `~/.asoundrc` | ALSA→PulseAudio config | Routes audio through WSLg |
| `~/.claude.json` | MCP server registration | VoiceMode MCP + env vars (STT/TTS URLs) |
| `~/.claude/settings.json` | Permission allow-list | Auto-allows converse, service, switch_mode tools |
| `~/.bashrc` | OPENAI_API_KEY export | Makes key available in shell (optional) |
| `~/.local/bin/voicemode-switch` | Symlink | Puts `voicemode-switch` in PATH |
| `~/.voicemode-local/config` | Install config | Stores install mode and Piper preference |

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
