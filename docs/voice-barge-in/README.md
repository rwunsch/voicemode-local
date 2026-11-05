# Voice barge-in: stop the assistant when you talk over it

Opt-in. Upstream `converse` has no equivalent as of 9.0.2 (issue #211 is open; its
`mouth`/`listen` packages have barge-in, but nothing connects them to `converse`).

```bash
voicemode-switch barge on       # downloads the Silero model, starts the trigger
voicemode-switch barge off      # takes effect immediately
voicemode-switch barge status
```

From inside Claude Code, Copilot or any MCP client, use the `voice_barge` tool with `on`,
`off` or `status`. `ensure` adds it to `VOICEMODE_TOOLS_ENABLED` if you have a whitelist.

Voice-mode processes started before the patch ignore it, so run `/mcp` to reconnect.

For a program that needs the state without a shell (FELIX reads it at voice start):
`python -m voice_mode.voice_barge status` prints one line, `on`, `off` or
`cannot run: <reason>`, and exits 0. It reads `VOICEMODE_VOICE_BARGE` from
`~/.voicemode/voicemode.env` (upstream's home, whatever `VOICEMODE_BASE_DIR` says), and
from the environment when the file does not set it. Today it says `cannot run` wherever
`parec` is missing, which includes native Windows and macOS.

## How it works

1. `patch_voice_barge.py` adds `speaking` and `listen_started_at` to the control
   channel's status reply, and a `barge_in` command that acts only while TTS plays.
   Plain `skip_forward` with nothing playing would end the next turn; `barge_in`
   checks under the state lock, so a late detection cannot.
2. `voice_barge.py` is one process per machine. It opens the mic only while `speaking`
   is true, and fires when Silero and a volume gate agree for 192 ms.
3. On a voice cut, `converse` skips the pause and chime. The trigger hands over the last
   3 s of mic audio up to the moment recording starts, trimmed by timestamp, and
   `converse` prepends it. The file (`~/.voicemode/barge_preroll.bin`) is mode 0600 and
   deleted after use.

Silero alone is not enough on speakers: it correctly calls the assistant's echo speech.

| Setting | Default | Notes |
|---|---|---|
| `VOICEMODE_VOICE_BARGE_GATE_DB` | -40 | Raise it (-35) if speech cuts itself off on speakers |
| `VOICEMODE_VOICE_BARGE_MIN_SPEECH_MS` | 192 | 96 ms gave false triggers from echo at lower gates |
| `VOICEMODE_VOICE_BARGE_DEVICE` | `@DEFAULT_SOURCE@` | PulseAudio source |

## Measurements (2026-11-04, one person, WSLg, n=1 per row unless stated)

| Setup | Test | Result |
|---|---|---|
| Laptop speakers + mic | Normal count, before the handoff | 1 of 4 words |
| Laptop speakers + mic | Normal count, at -35 dBFS and 1 s lookback | 10 of 10 words; listen began 28 ms after the cut (log lines, not stream open) |
| Laptop speakers + mic | Soft count, -40 dBFS | 8 of 8 words, cut in about 1 s; voice peaked -15 to -31 dBFS |
| Laptop speakers + mic | Silent during TTS | No cut in 19 s and in 13.4 s (2 runs); echo stayed below the gate, so this does not test echo rejection |
| Headset | Talk over TTS | Fired about 0.9 s into playback; "8, 1, 2" kept. Most of the count predated TTS |

`barge_in` to the player's stop log line: 7 to 35 ms (n=3). Time to actual silence not measured.

## Known limits

- **Strong echo is untested live.** Echo on this laptop swung about 20 dB between runs for
  an unknown reason (median near -62 dBFS, later -82, same devices, both players). The
  -40 gate was only replayed against the loud recordings: 0 false triggers in 3.
- **A real whisper on speakers probably won't interrupt.** A stretch at -41 to -50 dBFS
  was missed under load, and sits at or below the gate.
- **Heavy load** was present in the headset and soft runs. Transcription slowed (see
  `VOICEMODE_STT_TIMEOUT_LOCAL`). Its effect on cut timing has not been measured.
- **Buffered and `turns[]` players only.** With `VOICEMODE_STREAMING_ENABLED=true` the
  trigger stays inert. Linux/WSL only: it needs `parec`.
- A 1 s pause ends the turn, as in any listen.
