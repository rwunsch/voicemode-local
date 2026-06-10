#!/usr/bin/env python3
"""Patch voice_mode/simple_failover.py to remove the silent OpenAI voice swap.

Upstream simple_tts_failover maps a local voice (e.g. af_sky) to an OpenAI voice
(nova) when it falls through to the OpenAI endpoint, so a Kokoro/Piper outage
silently switches the user to a cloud voice mid-conversation. voicemode-local
policy is "OpenAI last-resort, no silent swaps": OpenAI only serves voices it
owns; a local voice that can't be served locally fails loudly.

Anchor-based and idempotent — fails loudly if the upstream block drifts.
"""
import sys
from pathlib import Path

MARKER = "NO SILENT SWAP (voicemode-local)"

OLD = '''        # Select appropriate voice for this provider
        if provider_type == "openai":
            # Map Kokoro voices to OpenAI equivalents, or use OpenAI default
            openai_voices = ["alloy", "echo", "fable", "nova", "onyx", "shimmer"]
            if voice in openai_voices:
                selected_voice = voice
            else:
                # Map common Kokoro voices to OpenAI equivalents
                voice_mapping = {
                    "af_sky": "nova",
                    "af_sarah": "nova",
                    "af_alloy": "alloy",
                    "am_adam": "onyx",
                    "am_echo": "echo",
                    "am_onyx": "onyx",
                    "bm_fable": "fable"
                }
                selected_voice = voice_mapping.get(voice, "alloy")  # Default to alloy
                logger.info(f"Mapped voice {voice} to {selected_voice} for OpenAI")
        else:
            selected_voice = voice  # Use original voice for Kokoro'''

NEW = '''        # Select appropriate voice for this provider.
        # NO SILENT SWAP (voicemode-local): never substitute a local (Kokoro/Piper)
        # voice with an OpenAI one. OpenAI is strictly last-resort and only serves
        # voices it actually owns. A local voice that can't be served locally fails
        # loudly rather than quietly becoming a surprise cloud voice mid-conversation.
        # To use OpenAI TTS, request an OpenAI voice (alloy/echo/fable/nova/onyx/
        # shimmer/...) explicitly.
        openai_voices = ["alloy", "echo", "fable", "nova", "onyx", "shimmer",
                         "ash", "ballad", "coral", "sage", "verse"]
        if provider_type == "openai" and voice not in openai_voices:
            logger.info(
                f"Skipping OpenAI for non-OpenAI voice '{voice}' (no silent swap)")
            attempted_endpoints.append({
                'endpoint': f"{base_url}/audio/speech",
                'provider': provider_type,
                'voice': voice,
                'model': model,
                'error': (f"voice '{voice}' is a local voice; not substituting with "
                          f"an OpenAI voice. Request an OpenAI voice to use OpenAI TTS."),
                'error_details': None,
            })
            continue
        selected_voice = voice'''


def apply(target: Path) -> bool:
    src = target.read_text()
    if MARKER in src:
        print(f"  already patched: {target}")
        return True
    if OLD not in src:
        raise SystemExit(
            f"ANCHOR DRIFT: expected voice-mapping block not found in {target}. "
            "Upstream simple_failover.py changed — update patch_simple_failover.py.")
    target.write_text(src.replace(OLD, NEW, 1))
    print(f"  patched (no silent swap): {target}")
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        import voice_mode
        target = Path(voice_mode.__file__).parent / "simple_failover.py"
    apply(target)
