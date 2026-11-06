# Mac check: interrupt by voice through echo cancellation (FELIX-MAC)

What this checks on a real Mac, because the tests here can only fake CoreAudio and
AVFoundation: that the route is read right, that voice processing starts in the trigger,
that channel 0 of its tap is echo-free, and that a talk-over still interrupts.

Run it on the branch with commit "feat(barge): macOS speakers go through voice processing".
Report every line asked for below, as printed, plus the macOS version (`sw_vers`).

## 1. Install

```bash
git pull                                  # the branch with this change
./install-mac.sh --barge --yes
```

Report the line that starts `pyobjc-framework-AVFoundation` (version and the MB it added,
or the warning if it did not install). Then reconnect voice mode in Claude Code (`/mcp`).

Check the package is there:

```bash
.venv/bin/python -c "import AVFoundation, importlib.metadata as m; print(m.version('pyobjc-framework-AVFoundation'), m.version('pyobjc-core'))"
```

## 2. Route check (no voice needed)

With the built-in speakers as output (headphones unplugged, Bluetooth off or disconnected):

```bash
.venv/bin/python -m voice_mode.voice_barge status
```

Expected: `on`, then `route: speakers, capture: voice-processing`.

Repeat with (a) wired headphones in the jack, if you have them, and (b) the Jabra
Bluetooth headset as output. Expected for both: `route: headphones, capture: sounddevice`.
Report the three outputs. A `route: unknown` anywhere is a finding: say which output.

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
roughly how long after you started talking.

## 4. Headphones (Bluetooth headset)

Switch output to the headset. Check `status` says `route: headphones`. Then one
**talk-over** as in step 3.3.

## 5. Lines to report

```bash
grep -E "stretch:|barge-in sent|barge: |voice processing|macOS:" /tmp/voice-barge.log | tail -30
```

Every `stretch:` line for steps 3 and 4. Each should end with `route=…, capture=…`:
`route=speakers, capture=voice-processing` for step 3, `route=headphones, capture=sounddevice`
for step 4. For the silent runs, the `max` and `p90` are the echo left after voice
processing; compare them with the gate (-40 dBFS by default).

Also report:

- Any line containing `voice processing tap failed` or `echo cancellation did not start`
  (with the traceback lines after it).
- Whether you heard Kokoro dip in volume when a stretch opened (ducking).
- Whether the first word of the talk-over was kept in the transcript.

## 6. If the silent runs still cut themselves off

That points at channel 0 not being the processed signal (`VP_CHANNEL` in
`patches/voice_barge.py`, marked unverified). Report the `stretch:` maxima; do not change
the code in the field. A one-off way to compare channels is a separate step for the next
round.

## Clean-up

Nothing to undo: interrupt by voice stays on. To switch it off: `voicemode-switch barge off`.
