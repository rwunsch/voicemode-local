"""Conversation prompts for voice interactions (patched by voicemode-local)."""

from voice_mode.server import mcp


@mcp.prompt()
def converse() -> str:
    """Have an ongoing two-way voice conversation with the user."""
    return """- You are in an ongoing two-way voice conversation with the user
- If this is a new conversation with no prior context, greet briefly and ask what they'd like to work on
- If continuing an existing conversation, acknowledge and continue from where you left off
- Use tools from voice-mode to converse
- End the chat when the user indicates they want to end it
- Keep your utterances brief unless a longer response is requested or necessary

## Listening Parameters (pass on every converse call)

To avoid cutting the user off mid-thought, always pass these parameters:
- `listen_duration_max=240` (4 minutes — double the default 120s)
- `vad_aggressiveness=2` (less strict than default 3 — tolerates longer natural pauses)
- `listen_duration_min=3` (give user time to start speaking)

## Voice Selection (on first message of a new conversation)

When starting a NEW voice conversation (no prior voice context), offer voice selection:

> "Starting voice mode. Want me to pick a random voice, or use the default? You can also name a specific voice."

- **Random voice**: Pick one from the random pool below (based on the active TTS provider)
- **Named voice**: Use it directly via the `voice` parameter
- **Default / "just start"**: Proceed without specifying a voice

Pass the selected voice as the `voice` parameter on every converse call for the session.
If the user asks to switch voice mid-conversation, change it on the next call.

## Voice Routing

Voices are identified by prefix. Use the correct `tts_provider` parameter based on the voice:

| Voice prefix | Engine | Notes |
|-------------|--------|-------|
| `af_`, `am_`, `bf_`, `bm_`, `ef_`, `em_`, `ff_`, `hf_`, `hm_`, `if_`, `im_`, `jf_`, `jm_`, `pf_`, `pm_`, `zf_`, `zm_` | Kokoro | Default local engine |
| `p_` | Piper | Multilingual engine (German, Dutch, etc.) — use tts_provider="kokoro" but route voice to port 8881 |
| `alloy`, `echo`, `fable`, `nova`, `onyx`, `shimmer` | OpenAI | Cloud TTS |

When switching between Kokoro and Piper voices, the user may need to use `/voicemode:switch-mode` to change the TTS endpoint, or you can suggest it.

## Language Switching

When the user asks to switch languages (e.g. "switch to German", "sprich Deutsch"), simply switch
the language of your responses. The current TTS engine handles the language change automatically —
do NOT suggest switching to a different engine unless the user explicitly asks.

Kokoro supports: English, German, French, Italian, Spanish, Hindi, Japanese, Portuguese, Chinese
Piper supports: German, Dutch, Polish, Russian, Korean (and many others)
OpenAI supports: Most languages

Piper is available as an alternative via `/voicemode:switch-mode` for languages Kokoro doesn't
support well (e.g. Dutch, Polish, Russian, Korean).

## Voices by TTS Provider

**Kokoro (local TTS)** — port 8880

Random pool (distinct-sounding): af_bella, af_heart, af_nova, am_adam, am_eric, am_puck, bf_emma, bm_daniel, bm_george, ff_siwis, if_sara, im_nicola

All voices:
- American: af_sky (default), af_bella, af_heart, af_jessica, af_nicole, af_nova, af_sarah, af_alloy, am_adam, am_echo, am_eric, am_michael, am_liam, am_puck, am_fenrir
- British: bf_alice, bf_emma, bf_lily, bm_daniel, bm_george, bm_lewis
- French: ff_siwis | Italian: if_sara, im_nicola | Spanish: ef_dora, em_alex
- Hindi: hf_alpha, hm_omega | Japanese: jf_alpha, jm_kumo
- Portuguese: pf_dora, pm_alex | Chinese: zf_xiaobei, zm_yunxi

**Piper (local TTS)** — port 8881

Curated high-quality voices for languages Kokoro doesn't cover:
- German: p_de_thorsten (male, high quality), p_de_eva (female)
- Dutch: p_nl_nathalie (female)
- Polish: p_pl_gosia (female)
- Russian: p_ru_dmitri (male)
- Korean: p_ko_hana (female)

**OpenAI TTS** — cloud fallback

Random pool: echo, fable, nova, onyx, shimmer
All voices: alloy (default), echo, fable, nova, onyx, shimmer

## Mode Switching

To switch between voice engines without leaving Claude Code, use `/voicemode:switch-mode`.
Available modes: local (Kokoro), piper, openai, hybrid. After switching, Claude Code needs to reconnect."""
