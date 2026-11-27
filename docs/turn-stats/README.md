# Per-turn numbers and the "still listening" note (FELIX #2107)

## Why
A field report (2026-11-27, Mac) said listening did not end on its own; the person pressed Stop,
which then cancelled a later turn (#2094). Whether that was background noise, a setting, or
something else could not be told: voice-mode logs only a recording's length. These numbers
answer that question for the next report, without keeping any audio or text.

## What the RECORDING_END event now carries
In `<base dir>/logs/events/voicemode_events_<date>.jsonl`, event `RECORDING_END`, under `data`. The
fields are always present, and null when unknown:

| field | meaning |
|---|---|
| `duration`, `samples` | upstream's fields, unchanged |
| `end_reason` | how the recording ended, see the table below |
| `frames` | 30 ms frames seen |
| `speech_share` | share of frames webrtcvad called speech, after the first speech frame (0..1) |
| `pause_resets` | pauses of 300 ms or more that a speech frame cut short (the turn went on) |
| `longest_pause_ms` | the longest non-speech run after the first speech |
| `quiet_dbfs` | median level of the non-speech frames, in dBFS: about -90 (silent) to 0 (loud). A noisy room shows here |
| `silence_needed_ms` | the pause that would have ended the turn (the silence threshold in force) |

| `end_reason` | the loop ended because |
|---|---|
| `silence` | a pause reached `silence_needed_ms` (the normal end) |
| `done` | the pill's Done (control `skip_forward`) |
| `ptt_release` | the push-to-talk key was released |
| `cap` | speech went on to `listen_duration_max` + `VOICEMODE_LISTEN_OVERRUN` (up to 420 s by default) |
| `no_speech` | no speech within `listen_duration_max` |
| `stop` | Voice off / a control stop |
| `pause` | the pill's Pause / Pause mic (the recording is discarded) |
| `skip_back` | skip back (replay) |
| `cancel` | the caller was cancelled (ESC) |
| `stall` | no audio arrived for 5 s (dead stream) |
| `device_error` | the audio device reported an error |
| `error` | an error while processing a chunk |
| `no_vad` | recorded without silence detection (disabled, or webrtcvad missing) |

Reading them: a turn that ended `cap` with a high `quiet_dbfs` (say above -40) and a high
`speech_share` points at noise that webrtcvad takes for speech. A turn that ended `done` after a
long time with many `pause_resets` shows that the person had to end it by hand.

**Nothing else is kept:** no audio, no text, no file names. The numbers are counted per frame inside
the recording loop. That counting is guarded and can never break a recording.

## The pill note
After `VOICEMODE_PILL_STILL_LISTENING_S` seconds of listening (default 30; 0 = off; an invalid value
means 30), the pill's status line says "Still listening. Press ↵ Done when you have finished". It
is text only. A sound would be recorded by the microphone, could count as speech (and keep the
turn open), and Whisper would hear it. The turn never ends because of the note.

Why 30 s: on this machine the median turn was 17 s (n=564 local recordings, 2026-09..11), so the
note does not show for most turns. Long monologues see it too, which is harmless.

## Files
- `patches/turn_stats.py` (copied into `voice_mode/`): counting and the fixed fields.
- `patches/patch_turn_stats.py`: wires it into `record_audio_with_silence_detection_inner` and
  the `RECORDING_END` event (`tools/converse.py`); both manifests.
- `patches/voice_pill.py` (`still_listening` in the snapshot, `still_after()`), `ui/pill/model.js`
  (`STILL_LISTENING_TEXT`).
- Tests:
  - `tests/test_turn_stats.py`;
  - `tests/test_turn_stats_patch.py`, which runs the patched loop with a fake microphone and a
    scripted VAD and checks silence, no_speech, cap, done, stop and no_vad, on both lists;
  - `tests/test_voice_pill.py` and `tests/pill_model.test.mjs` for the note.

## What we learned before building this (offline, 2026-11-28)
All 564 of Robert's local recordings were replayed through 8.12.0's loop.
- Isolated single false "speech" frames: 1 in total, so they are not the mechanism.
- Per-chunk resampling: no effect.
- A "90% of the last second is quiet" rule would have ended turns a median 6.6 s early, with real
  speech after its end in 451 of 564 turns, so it was rejected.

His room is mostly quiet (median quiet level -68 dBFS), so a noisy room is barely represented.
Silero as an end-of-turn detector comes later, in shadow mode, after the move to 9.2.0
(`docs/upstream/move-to-9.2.0-assessment.md`). Silero, too, will call background talkers speech.
