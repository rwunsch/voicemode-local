# Mac check: interrupt by voice through echo cancellation (FELIX-MAC)

What this checks on a real Mac, because the tests here can only fake CoreAudio and
AVFoundation: that the route is read right, that voice processing starts in the trigger,
that channel 0 of its tap is echo-free, and that a talk-over still interrupts.

Run it on the branch with commit "feat(barge): macOS speakers go through voice processing"
and the fix-round-1 commit after it. Report every line asked for below, as printed.

First, the macOS version:

```bash
sw_vers
```

## 1. Install

```bash
git pull                                  # the branch with this change
./install-mac.sh --barge --yes
```

Report the line that starts `pyobjc-framework-AVFoundation` (version and the MB it added,
or the warning if it did not install). Then reconnect voice mode in Claude Code (`/mcp`).

List every pyobjc package and its version (all should share one release):

```bash
uv pip list --python .venv/bin/python | grep -i pyobjc
```

## 2. Route check (no voice needed)

With the built-in speakers as output (headphones unplugged, Bluetooth off or disconnected):

```bash
.venv/bin/python -m voice_mode.voice_barge status
```

Expected: `on`, then `route: speakers, capture: voice-processing`.

Repeat with (a) wired headphones in the jack, if you have them, and (b) the Jabra
Bluetooth headset as output. Expected for both: `route: headphones, capture: sounddevice`.
If you have an output with no mic (external USB or Bluetooth speakers, a display over HDMI,
AirPlay), select it too. Expected: `route: speakers, capture: voice-processing`. Report the
device and what `status` said: the headset/speaker rule is untested on real devices.
Report the three outputs. A `route: unknown` anywhere is a finding: say which output.

With the built-in speakers again, `voicemode-switch barge status` should end with the
same `route: speakers, capture: voice-processing` line. Report it.

Restart the trigger so it picks up the new code, then confirm it started:

```bash
voicemode-switch barge off && voicemode-switch barge on
grep "macOS:" /tmp/voice-barge.log | tail -1
```

Expected: `macOS: VOICEMODE_VOICE_BARGE_ECHO=auto, echo cancellation (AVFoundation) available, route now speakers`.

## 3. Built-in speakers, volume as you normally use it

Start `/voicemode:converse` and ask for a long answer (30 s or more), so each stretch is long.

1. **Silent run 1.** Say nothing while it speaks. Let it finish.
2. **Silent run 2.** The same again.
3. **Talk-over.** Halfway through a long answer, say a full sentence at normal volume
   ("stop, I have a question about the second point").

For each, report: did it cut itself off (silent runs) or stop for you (talk-over), and
roughly how long after you started talking. Also, **per stretch**: any click, gap or
dropout in Kokoro's voice when the mic opened (stretch start) or closed (stretch end).

## 4. Headphones (Bluetooth headset)

Switch output to the headset. Check `status` says `route: headphones`. Then one
**talk-over** as in step 3.3.

## 5. Lines to report

```bash
grep -E "stretch:|barge-in sent|barge: |voice processing|macOS:" /tmp/voice-barge.log | tail -30
```

Every `stretch:` line for steps 3 and 4. Each should end with
`first_window <ms>, deaf <ms>, warm <ms>` (or `cold`) `, route=…, capture=…`: `route=speakers, capture=voice-processing` for
step 3, `route=headphones, capture=sounddevice` for step 4. For the silent runs, the `max`
and `p90` are the echo left after voice processing; compare them with the gate
(-40 dBFS by default).

Check on each voice-processing stretch line:

- **Window count.** `windows` should be about 31 per second of the stretch (32 ms
  windows), 6 fewer on a `cold` stretch (the warm-up windows). About a quarter of that means the
  tap read bytes as samples (pyobjc's `as_buffer` counted bytes): report it as a finding.
- **`deaf` and `warm`.** The mic now opens while the reply is generated. `deaf` is how
  long into the reply the detector heard nothing; before this change it was about 1 s
  (`first_window` 801-923 ms, n=6, plus the 192 ms warm-up). Report every `deaf` and `warm`
  value, for both captures. A `cold` stretch's `first_window` is still the start-up cost.
- **Talk over the first second.** On the speakers, start talking as soon as the reply
  starts: it should cut within about 0.3 s (192 ms of speech plus `deaf`). Report n and
  how many cut. Also say whether the recording after the cut kept your first words.

Also report:

- Any `voice processing tap failed` line, with the traceback after it, and the
  `tap failed N times` line at the end of that capture.
- Any `echo cancellation did not start` line, and what `voicemode-switch barge status` said
  right after it (expected: `capture: refused (echo cancellation failed to start)`).
- Whether `ducking not settable` appeared (`grep "ducking not settable" /tmp/voice-barge.log`).
- Whether you heard Kokoro dip in volume when a stretch opened (ducking).
- Whether the first word of the talk-over was kept in the transcript.

## 5b. Which channel is the echo-cancelled one

One silent stretch on the speakers with the channel probe on, to settle `VP_CHANNEL`:

```bash
echo 'VOICEMODE_VOICE_BARGE_VP_CHANNEL_PROBE=1' >> ~/.voicemode/voicemode.env
voicemode-switch barge off && voicemode-switch barge on
```

Ask for a long answer and say nothing. Then:

```bash
grep "vp channel probe:" /tmp/voice-barge.log | tail -1
```

Report the line: the level of every tap channel over that stretch. The processed channel
should be far quieter than raw mic channels while only the assistant is talking. Then
remove the line from `~/.voicemode/voicemode.env` and run `barge off` and `barge on` again.

## 6. If the silent runs still cut themselves off

That points at channel 0 not being the processed signal (`VP_CHANNEL` in
`patches/voice_barge.py`, marked unverified). Report the `stretch:` maxima and the probe
line from 5b; do not change the code in the field.

## Clean-up

Nothing to undo: interrupt by voice stays on. To switch it off: `voicemode-switch barge off`.
