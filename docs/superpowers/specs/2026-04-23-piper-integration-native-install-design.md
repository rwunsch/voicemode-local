# Piper TTS Integration & Native Install Mode

**Date:** 2026-04-23
**Status:** Draft
**Scope:** Add Piper TTS as an alternative voice engine with language fallback, add native (non-Docker) install mode, add in-session mode switching.

## Problem

1. Kokoro TTS lacks support for several languages, notably German. Users who need multilingual voice output have no local option.
2. The current install requires Docker, which excludes users on systems without Docker Desktop (especially macOS users who find Docker Desktop heavy).
3. Mode switching (local/openai/hybrid) requires exiting Claude Code and running a terminal command.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Piper integration pattern | Separate OpenAI-compatible proxy (port 8881) | Follows same pattern as Kokoro; modular; works with both install modes |
| Piper execution model | On-demand process | Piper is fast enough (<200ms generation); saves resources vs persistent service |
| Install modes | Docker (default) + native fallback | Docker preferred for isolation; native for systems without Docker |
| Language fallback | Explicit user confirmation | User asked for this; avoids surprising engine switches |
| Voice curation | Curated high-quality subset | Quality over quantity; users don't want to wade through hundreds of voices |
| Mode switching | MCP tool (patch) + upstream PR | In-session switching is essential UX; upstream PR for long-term |

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Claude Code + VoiceMode MCP                                       │
│    STT_BASE_URL → http://127.0.0.1:2022/v1    (local Whisper)     │
│    TTS_BASE_URL → http://127.0.0.1:8880/v1    (Kokoro, default)   │
│                   http://127.0.0.1:8881/v1    (Piper, alt)        │
│                   (none)                       (OpenAI, cloud)     │
└──────┬───────────────────┬────────────────┬────────────────────────┘
       │                   │                │
       ▼                   ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ whisper-proxy │  │ Kokoro TTS   │  │ piper-proxy  │
│ :2022         │  │ :8880        │  │ :8881        │
│ (Python)      │  │ (Docker or   │  │ (Python)     │
│               │  │  native)     │  │ on-demand    │
│ Translates:   │  │ OpenAI-compat│  │ OpenAI-compat│
│ /v1/audio/    │  │ /v1/audio/   │  │ /v1/audio/   │
│ transcriptions│  │ speech       │  │ speech       │
│       │       │  └──────────────┘  │ voices       │
│       ▼       │                    │       │      │
│ faster-whisper│                    │       ▼      │
│ :9000         │                    │ piper-tts    │
│ (Docker/native│                    │ (Python lib) │
└──────────────┘                    └──────────────┘
```

## Components

### 1. piper-proxy (new)

A lightweight Python HTTP server exposing OpenAI-compatible TTS endpoints on port 8881.

**Endpoints:**
- `POST /v1/audio/speech` — Generate speech from text using Piper. Accepts `voice`, `input`, `speed` parameters. Returns audio data (WAV or MP3).
- `GET /v1/audio/voices` — List available curated Piper voices with language and quality metadata.
- `GET /v1/models` — Return model list for provider discovery.
- `GET /health` — Health check.

**Implementation:**
- Single Python file (`piper-proxy.py`), same style as `whisper-proxy.py`
- Uses `piper-tts` Python package for synthesis
- On-demand: starts when `voicemode-switch start` runs, stops on `stop`. Not a persistent daemon — does not auto-start at boot. Starts quickly (~1s) so no warm-up penalty.
- Voice models are downloaded on first use and cached in `models/piper/`

**Voice naming convention:**
- Piper voices use prefix `p_` followed by language and name: `p_de_thorsten`, `p_fr_siwis`, etc.
- This clearly distinguishes them from Kokoro voices (`af_`, `bm_`, etc.) and OpenAI voices (`alloy`, `echo`, etc.)

### 2. Curated Piper Voices

Focus on high-quality voices for languages Kokoro doesn't cover well:

| Voice ID | Language | Gender | Piper Model | Quality |
|----------|----------|--------|-------------|---------|
| `p_de_thorsten` | German | Male | `de_DE-thorsten-high` | High |
| `p_de_eva` | German | Female | `de_DE-eva_k-x_low` (verify model name) | Medium |
| `p_nl_nathalie` | Dutch | Female | `nl_NL-nathalie-high` | High |
| `p_pl_gosia` | Polish | Female | `pl_PL-gosia-high` | High |
| `p_ru_dmitri` | Russian | Male | `ru_RU-dmitri-medium` | Medium |
| `p_ko_hana` | Korean | Female | `ko_KR-x_medium` | Medium |

Additional voices may be added. The voice list lives in a config file (`voices/piper-voices.json`) for easy updates.

Note: Languages already well-served by Kokoro (English, French, Italian, Spanish, Japanese, Chinese, Hindi, Portuguese) are not duplicated in Piper unless quality is significantly better.

### 3. Install Modes

`install.sh` prompts at the start:

```
How would you like to install voice services?
  1) Docker (recommended) — uses Docker containers
  2) Native — installs directly on your system
  
  [1]:
```

Auto-detects Docker availability and recommends accordingly.

**Config file:** `~/.voicemode-local/config` stores the install mode:
```
INSTALL_MODE=docker    # or "native"
PIPER_ENABLED=true     # whether Piper is installed
```

#### Docker mode (existing, extended)

- `docker-compose.yml` gains an optional `piper` service
- Piper container image: `rhasspy/wyoming-piper` or custom Dockerfile
- piper-proxy still runs as a host-side Python process (translates OpenAI API to Piper)

#### Native mode (new)

- Creates a Python virtual environment at `~/.voicemode-local/venv/`
- Installs: `faster-whisper`, `piper-tts`, `kokoro` (if available natively)
- Services run as background processes managed by PID files in `/tmp/`
- `voicemode-switch start` launches processes; `stop` kills them

### 4. voicemode-switch Updates

New mode added:

| Mode | STT | TTS | Cost |
|------|-----|-----|------|
| `local` | Local Whisper | Local Kokoro | Free |
| `piper` | Local Whisper | Local Piper | Free |
| `openai` | OpenAI | OpenAI | ~$0.01/min |
| `hybrid` | OpenAI STT | Local Kokoro | ~$0.006/min |

New commands:
- `voicemode-switch piper` — Switch TTS to Piper (STT stays local Whisper)
- `voicemode-switch start` — Now also starts piper-proxy if Piper is enabled
- `voicemode-switch stop` — Now also stops piper-proxy

### 5. Mode Switching MCP Tool (patch)

A new MCP tool patched into the voicemode package, accessible as `/voicemode:switch-mode`.

**Parameters:**
- `mode`: One of `local`, `piper`, `openai`, `hybrid`

**Behavior:**
1. Updates `~/.claude.json` MCP env vars (STT_BASE_URL, TTS_BASE_URL, TTS_VOICE)
2. Starts/stops local services as needed
3. Returns a message indicating the switch and that Claude Code needs reconnection

**Upstream PR:** After validating the patch works, submit a PR to the voicemode package to add mode switching natively. The PR should include the tool definition and a generic provider registry that supports arbitrary TTS/STT endpoints.

### 6. Converse Prompt Patch Update

The existing `patches/converse.py` is extended with:
- Piper voice list section
- Language fallback instructions:
  > When the user requests a language not available in the current TTS engine, inform them and offer to switch. Example: "German isn't available in Kokoro. Want me to switch to Piper? I'd recommend p_de_thorsten, a natural-sounding German voice."
- Voice engine awareness: Claude checks the voice prefix to route to the correct TTS endpoint (`af_`/`bm_` → Kokoro on 8880, `p_` → Piper on 8881, bare names → OpenAI)

### 7. Voice Routing in the Converse Prompt

When a voice is selected, Claude determines which TTS provider to use based on the voice name prefix:

| Voice prefix | Engine | TTS endpoint |
|-------------|--------|-------------|
| `af_`, `am_`, `bf_`, `bm_`, `ef_`, `em_`, `ff_`, `hf_`, `hm_`, `if_`, `im_`, `jf_`, `jm_`, `pf_`, `pm_`, `zf_`, `zm_` | Kokoro | http://127.0.0.1:8880/v1 |
| `p_` | Piper | http://127.0.0.1:8881/v1 |
| `alloy`, `echo`, `fable`, `nova`, `onyx`, `shimmer` | OpenAI | (default, no base URL) |

Claude uses the `tts_provider` parameter on `mcp__voicemode__converse` to target the correct endpoint when the voice prefix indicates a non-default engine. This routing logic lives in the converse prompt patch (Claude's behavioral instructions), not in application code.

## File Changes Summary

| File | Change |
|------|--------|
| `piper-proxy.py` | **New** — OpenAI-compatible TTS proxy for Piper |
| `voices/piper-voices.json` | **New** — Curated Piper voice catalog |
| `docker-compose.yml` | Add optional Piper service |
| `install.sh` | Add install mode selection, native install path, Piper setup |
| `voicemode-switch` | Add `piper` mode, Piper service management |
| `patches/converse.py` | Extend with Piper voices, language fallback, voice routing |
| `patches/switch_mode.py` | **New** — MCP tool for in-session mode switching |
| `patches/apply.sh` | Apply new patches (switch_mode.py) |
| `CLAUDE.md` | Update voice lists and documentation |
| `README.md` | Update architecture diagram, add Piper section |

## Out of Scope

- Training custom voice models
- Real-time voice cloning
- Simultaneous multi-engine TTS (one active engine at a time, with prompt-level routing for language fallback)
- Speech-to-speech translation
- Piper STT (Piper is TTS only; Whisper handles STT)
