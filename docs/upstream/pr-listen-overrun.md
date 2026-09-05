# Draft: speech is truncated mid-word at `listen_duration_max`

**Type:** bug → PR. **Target:** `mbailey/voicemode` `master`. **Status:** not submitted.
**Post the issue first, link the PR to it.**

---

## Issue body

**Title:** `Recording is hard-cut at max_duration even while the user is still speaking`

A user who is still talking when `listen_duration_max` elapses is cut off mid-word. The
recording ends at exactly the cap, the transcript ends mid-sentence, and the agent replies
to half a thought.

**Where.** `voice_mode/tools/converse.py:1467`:

```python
while (recording_duration < max_duration and not stop_recording
       and time.monotonic() - last_audio_time < AUDIO_STALL_TIMEOUT):
```

`max_duration` is a hard ceiling on the whole recording, applied regardless of whether
speech is currently in progress.

**This is not the stall backstop.** The comment directly above it is explicit that the
8.11.0 backstop is deliberately *not* a length cap:

> This is a dead-stream safety net, NOT a cap on recording length: `last_audio_time` is
> bumped on every chunk, so a healthy (even slow) recording is never truncated — length
> is still governed by `recording_duration < max_duration`.

That is exactly the case being reported: a *healthy* recording, truncated — not by the
backstop, by `max_duration` itself. Line 1559 confirms the intent (`The only exit is
speech detection or max_duration`).

**Why the cap is the wrong instrument.** `listen_duration_max` is chosen by the calling
agent before it knows how long the human will speak. Its job is to stop an *idle* mic
running forever. Using it to also bound *active speech* conflates "nobody is talking" with
"somebody is still talking", and only the first is a runaway condition worth truncating.

**Observed.** Recordings ending at exactly 60.0s and 120.0s of samples (matching the two
agent-chosen caps), transcripts ending mid-word. Reproducible by setting a short
`listen_duration_max` and talking past it.

**Reproduce.**
```bash
voicemode converse --listen-duration-max 10 "Please read a long paragraph aloud."
# keep talking past 10s -> the transcript stops mid-word at the cap
```

**Suggested fix.** Once `speech_detected` is true, let the normal silence exit end the
recording rather than the cap, bounded by a second, generous safety ceiling so a stuck-open
mic still terminates. In our fork that ceiling is `max_duration + VOICEMODE_LISTEN_OVERRUN`
(default 300s). Silence detection already provides the natural end; the cap only needs to
survive as a backstop for the *no speech at all* case, which it already handles at :1550.

Happy to open a PR — we've run this shape in production for ~3 months.

---

## PR body

**Title:** `fix: don't truncate active speech at listen_duration_max`

Fixes #<ISSUE>.

`max_duration` is applied as a hard ceiling on the whole recording, so a user still
speaking when the agent-chosen `listen_duration_max` elapses is cut off mid-word
(`converse.py:1467`). The 8.11.0 stall backstop explicitly does not cover this — its own
comment says length is still governed by `recording_duration < max_duration`.

**Change.** Once `speech_detected` is true, the loop bound relaxes from `max_duration` to
`max_duration + VOICEMODE_LISTEN_OVERRUN` (new env var, default 300s), leaving the normal
silence exit to end the recording. Before speech is detected, behaviour is byte-for-byte
unchanged — the cap still terminates an idle mic at exactly `max_duration`.

**Why an env var and not unbounded.** A microphone that never goes silent (a fan, an open
Teams call, a stuck VAD) must still terminate. 300s is generous enough that no human
utterance reaches it and short enough to bound the failure.

**Interaction with the stall backstop.** None — `AUDIO_STALL_TIMEOUT` is orthogonal and
untouched; a dead stream still ends in 5s whether or not speech was detected.

**Tests.** Speech-in-progress at the cap extends; no-speech at the cap still terminates at
`max_duration`; overrun ceiling terminates a never-silent stream.

**Notes for the reviewer.** The one-line loop condition is the whole behavioural change;
the rest is the ceiling bookkeeping and the config plumbing. Reviewed against 8.12.0.
