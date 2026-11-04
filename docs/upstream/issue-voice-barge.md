# Draft: voice barge-in for `converse`, and whether a PR is wanted

**Type:** report + question → possible PR. **Target:** `mbailey/voicemode`. **Status:** not submitted.
**Where:** a comment on #211 or a new issue linking it. Copy only the text under "Body". No @mentions.
Fact-checked against the code and session logs by an independent reviewer, 2026-11-04.

---

## Body

**Title (if a new issue):** `Voice barge-in for converse: a working implementation, and is a PR wanted?`

Related: #211. I have voice barge-in working on 8.12.0 with my local patches. I'm writing up how it
works in case it's useful, and to ask whether you'd want it as a PR, given where `mouth` and `listen`
are heading.

It adds a control-channel change, edits to the players and listen path in `core.py` and `converse.py`,
and a separate trigger process. It currently sits on top of my local push-to-talk hold patch, so a PR
would need separating from that.

Code: [`patch_voice_barge.py`](https://github.com/rwunsch/voicemode-local/blob/main/patches/patch_voice_barge.py),
[`voice_barge.py`](https://github.com/rwunsch/voicemode-local/blob/main/patches/voice_barge.py).

**How it works**

1. **Two status fields and one command on the control channel.** Status gets `speaking`, set around
   the buffered and `turns[]` players, and `listen_started_at`. The new `barge_in` command is
   `skip_forward`, but it only acts while TTS is playing, checked under the state lock. Plain
   `skip_forward` from a client racing the end of playback would latch and end the next listen.
2. **A separate trigger process.** One per machine. It polls status, opens the mic only while
   `speaking` is true, and sends `barge_in` once when Silero VAD and a volume gate agree for 192 ms.
   `converse` behaves as before when it isn't running.
3. **A handoff, so the first words survive.** On a voice cut, `listen_and_transcribe` skips the pause
   and chime and treats the stream as speech already in progress. The trigger keeps its last 3 s of
   audio and captures until `listen_started_at` appears. `converse` then prepends that audio, trimmed
   by timestamp so nothing is doubled.

**Why the volume gate.** On speakers the mic hears the assistant, and Silero correctly calls that
speech. With Silero alone and a 96 ms minimum, I got 8 false triggers in a 20 s echo-only clip, and 8,
0 and 8 in three talk-over runs before I spoke. With the gate, at 96 or 192 ms, replay gave none,
though only two of those three runs had echo for it to reject. Silero-flagged echo peaked near -38
dBFS, against a speech median of about -18. Echo did cross -40 briefly, and the 192 ms minimum is what
rejected it.

**What I measured** (one person, WSL2, laptop speakers and mic, n=1 per row unless stated)

| Test | Settings | Result |
|---|---|---|
| Normal count, no handoff | -35 dBFS | 1 of 4 words kept; listen began 1.05 s after the cut |
| Normal count, handoff | -35 dBFS, 1 s lookback | 10 of 10 words; listen began 28 ms after the cut |
| Softer count, handoff | -40 dBFS, 3 s lookback | 8 of 8 words, cut in about 1 s; voice peaked -15 to -31 dBFS; machine under load |
| Silent during TTS (2 runs) | Both | No cut in 19 s and 13.4 s, but echo stayed below the gate, so this does not test echo rejection |
| `barge_in` to the player's stop log line (n=3) | | 7 to 35 ms; time to actual silence not measured |

Listen times are gaps between log lines, not the moment the audio stream opened.

**Limits**

- It has not faced strong echo live. Echo here swung about 20 dB between runs for a reason I haven't
  found, so the gate was only replayed against the loud recordings.
- A real whisper on speakers probably won't interrupt.
- It's Linux/WSL only (`parec`). The streaming player doesn't report `speaking` yet, so I run with
  `VOICEMODE_STREAMING_ENABLED=false`.
- It's built against 8.12.0 and not yet rebased onto 9.x.

**Questions**

1. Would you want this as a PR, or does `mouth`/`listen` make it the wrong direction? I saw
   `mouth/hold.py` has word-based barge-in, and that `converse` doesn't speak through it yet. Happy to
   leave this alone if that's where it should land.
2. If yes, would you take the control-channel part on its own first? `speaking` and `barge_in` are
   useful to any client. The trigger could then follow, with Silero as an optional dependency.
   I also noticed the `turns[]` loop already writes a phase to `~/.voicemode/state.json`, so `speaking`
   might belong there instead. I haven't checked whether plain `converse` is covered.
