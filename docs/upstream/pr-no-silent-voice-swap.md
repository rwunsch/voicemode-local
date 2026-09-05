# Draft: local voice silently becomes an OpenAI voice on failover

**Type:** bug → PR. **Target:** `mbailey/voicemode` `master`. **Status:** not submitted.

---

## Issue body

**Title:** `Falling back to OpenAI silently substitutes a different voice`

When TTS failover reaches the OpenAI endpoint, the requested local voice is silently
remapped to an OpenAI voice. A Kokoro or Piper hiccup therefore doesn't surface as an
error — it surfaces as *the assistant suddenly speaking in a different voice*, and in a
paid one.

**Where.** `voice_mode/simple_failover.py:84-97`:

```python
openai_voices = ["alloy", "echo", "fable", "nova", "onyx", "shimmer"]
if voice in openai_voices:
    ...
    voice_mapping = {
        "af_sky": "nova",
        "af_sarah": "nova",
        "af_alloy": "alloy",
        ...
    }
selected_voice = voice_mapping.get(voice, "alloy")  # Default to alloy
```

**Why this is worth changing.**

1. **It is silent.** Nothing in the return value or the logs tells the user their voice was
   substituted. The first signal is auditory, mid-conversation.
2. **It converts a free local failure into a billed cloud call.** A user running local-only
   for cost reasons gets charged for a fallback they didn't ask for and weren't told about.
3. **It defeats voice-as-identity.** Running several agents concurrently, each on a distinct
   voice, the voice *is* how you tell sessions apart. Collapsing `af_sky` and `af_sarah`
   both onto `nova` merges two identities into one — silently.
4. **It masks the real fault.** The Kokoro outage that triggered the fallback goes
   unreported; the user debugs "why did the voice change" instead of "why is Kokoro down".

**Relationship to VM-1556.** 8.8.0 fixed the generated default `voicemode.env` leaving
`alloy` in play. That was the *config* path. This is the *runtime failover* path, and it
still substitutes.

**Reproduce.**
```bash
# Kokoro up, OpenAI configured as fallback
voicemode converse --voice af_sky "one"        # Kokoro, af_sky
docker stop voicemode-kokoro                    # or: voicemode service stop kokoro
voicemode converse --voice af_sky "two"        # OpenAI, nova -- no warning
```

**Suggested fix.** Pass the requested voice through unchanged. If the endpoint doesn't own
it, the request fails loudly and the user learns their local TTS is down — which is the
actual news. If a substitution is wanted, make it opt-in and announce it in the result.

---

## PR body

**Title:** `fix: don't silently substitute an OpenAI voice on TTS failover`

Fixes #<ISSUE>.

`simple_tts_failover` remaps a local voice to an OpenAI voice when it falls through to the
OpenAI endpoint (`simple_failover.py:84-97`), so a Kokoro/Piper outage silently switches
the user to a different — and billed — voice mid-conversation, with no log line and no
marker in the return value.

**Change.** The requested voice is passed through unchanged. An endpoint that doesn't own
the voice now fails loudly, surfacing the underlying outage instead of masking it.

**Opt-in preserved.** For anyone relying on the old behaviour, it stays reachable behind
`VOICEMODE_TTS_VOICE_SUBSTITUTION=true` (default `false`), and when it fires it logs the
substitution at WARNING rather than doing it silently.

**Why default-off.** A silent identity change is the kind of failure that costs more to
diagnose than the outage it hides — particularly for multi-agent setups where the voice is
the only way to tell two concurrent sessions apart.

**Blast radius.** One function. No config migration; users with no OpenAI key see no change
at all, since they never reached this path.

**Tests.** Voice passes through unchanged on fallback; substitution fires only when the flag
is set; substitution logs at WARNING when it does. Reviewed against 8.12.0.
