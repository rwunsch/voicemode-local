# Voice sensitivity: how early a turn ends, changed live

## What it is

Voice mode ends your turn when the voice activity detector (webrtcvad) has heard enough
silence. Two numbers decide it:

- **VAD aggressiveness**, 0-3. Higher means more background noise is counted as non-speech,
  so silence is detected sooner in a noisy room.
- **Silence length** in milliseconds: how long a pause ends the turn.

Upstream reads both once, at start (`VOICEMODE_VAD_AGGRESSIVENESS`,
`VOICEMODE_SILENCE_THRESHOLD_MS`, config.py:948-949 in 8.12.0). A running voice server never
sees a change to voicemode.env; `update_config` writes the file but needs a `/mcp` reconnect.

Why it was built (2026-11-18): at aggressiveness 2 and 2000 ms, with background noise, the
silence never reached 2 s, and the user had to mute the microphone to end a turn.

The `voice_sensitivity` MCP tool changes both **live**: the change applies at the **next
listen**, with no restart and no reconnect.

## Using it

Ask the assistant in plain words ("it reacts too late", "it keeps listening because of the
noise", "it cuts me off"), or call the tool:

| action | what it does |
|---|---|
| `status` | the configured values, the override if any, and the values in effect |
| `set` | a preset (`level`) or explicit numbers (`vad_aggressiveness`, `silence_ms`) |
| `reset` | removes the override; the configured values apply again |

### Presets

| preset | aggressiveness | silence ms | for |
|---|---|---|---|
| `earlier` | 3 | 1200 | end the turn sooner: background noise counts as silence |
| `noisy` | 3 | 800 | loud background: strictest speech check, shortest pause |
| `later` | 1 | 2500 | wait longer: pauses while thinking, quiet room |

Explicit numbers are checked: aggressiveness a whole number 0-3, silence 300-5000 ms.
Anything else is refused and nothing is written. Numbers alone change only the field given;
the other keeps its override value. A level with a number uses the preset with that field
replaced. `set` and `reset` are idempotent.

## The override file

`<base dir>/sensitivity.json`, where the base dir is `VOICEMODE_BASE_DIR`, else
`~/.voicemode`:

```json
{"vad_aggressiveness": 3, "silence_ms": 1200, "preset": "earlier"}
```

- Written atomically (temp file + replace in the same folder).
- Read at each recording, cached by mtime and size (a file changed in the last second is
  read again every time, since mtime moves on a coarse tick).
- Missing or corrupt file: no override. A field that is null, the wrong type or out of
  range: no override for that field. Nothing here raises into the recording.

## Order of precedence (per field, at each recording)

1. A `vad_aggressiveness` passed to `converse` for that call (aggressiveness only; silence
   length has no per-call argument).
2. The override file.
3. The configured value (voicemode.env / environment, else upstream's defaults 3 and 1000).

**With no override file the values are exactly the configured ones**: no default changed.

## How it is wired

- `patches/voice_sensitivity.py` -> `voice_mode/voice_sensitivity.py`: the store,
  `resolve()` and the tool's words (stdlib only).
- `patches/patch_vad_override.py` on `tools/converse.py`: inside
  `record_audio_with_silence_detection` only, upstream's effective-aggressiveness line
  calls `resolve()`, and every later `SILENCE_THRESHOLD_MS` in that function reads the
  resolved value (the silence exit, also as `patch_converse_hold` rewrites it, and its log
  lines). If the module is missing, the values are upstream's. Anchor drift exits 1 and
  writes nothing; idempotent by marker; keeps CRLF.
- The tool `voice_sensitivity` is in `patches/voice_barge_tool.py`, so every install that
  already loads `voice_barge_tool` gets it with no settings change. It imports the module
  only when called.
- Both `patches/manifest.json` and `patches/manifest.windows.json` carry the copy and the
  patch, after every other converse patch.

When an override is in effect, each recording logs
`Voice sensitivity override (sensitivity.json): aggressiveness N, silence M ms`.

## Reset by hand

Delete `<base dir>/sensitivity.json`, or call the tool with `reset`.

## What was not tested

- **The effect with a real microphone and background noise.** Tests cover the resolution
  order, the file handling and the patched recording function's threshold on synthetic
  input, and that the patch applies to pristine 8.12.0 and after the whole manifest. Whether
  `earlier` / `noisy` actually end a turn in a given noisy room has not been measured.
- The preset numbers are judgement, not measurement.
- Native Windows: covered by the Windows manifest tests on Linux, not run on Windows.
