# Multi-session voice queue — manual test

Reproducible test for the cross-session voice queue: proxy auto-start, multi-voice
routing, strict FIFO turn-taking, no talk-over (barge-in fix), and the
listen-window cap under contention.

## What it verifies

1. **Proxy auto-start** — the first of the sessions to load brings the proxies up
   via the `voicemode-mcp` wrapper (`~/.voicemode/logs/ensure.log`; empty = ran,
   proxies already up).
2. **Routing + distinct voices** — each session uses a different voice, proving
   per-voice routing through Kokoro.
3. **FIFO queue, no talk-over** — all sessions want the mic; they take strict
   turns, audio windows never overlap (a holder can't be interrupted mid-turn).
4. **QUEUED retry contract** — a waiting session re-calls with its `ticket`, never
   degrades to text.
5. **Listen-window cap** — under contention a silent holder yields in
   ~`VOICEMODE_QUEUE_LISTEN_CAP` (8s) instead of the full window.

## Setup

Open **3 Claude Code sessions** (a few seconds apart). They can share a folder
(the queue distinguishes them by voice) or use separate subfolders (distinct
project names). Restart sessions first if code changed since they were launched.

## The prompt — paste into each session, change only the first line

```
You are voice test session "A" and your voice is "af_sky".
( session B → label "B", voice "am_puck" ;  session C → label "C", voice "bf_emma" )

We are testing the voicemode multi-session FIFO queue. Do exactly this:

1. Set LABEL and VOICE from the line above. Note your folder: run `pwd`.
2. Append a START line to ./voicetest_$LABEL.log:
   printf '%s | %s | %s\n' "$(date -Ins)" "$LABEL" "START pid=$$" >> ./voicetest_$LABEL.log
3. Do 4 rounds. In each round i (1..4):
   a. Call mcp__voicemode__converse with:
        message = "This is session <LABEL>, round <i>. The time is <speak HH:MM:SS>. Over."
        voice = <VOICE>, wait_for_response = true, listen_duration_max = 15
   b. If converse returns "QUEUED — position N of M ...", immediately call converse
      again with the SAME message and the given ticket. Repeat until you get the
      floor. NEVER print the message as text, NEVER give up.
   c. When the converse call returns success (you spoke), append:
        printf '%s | %s | round %s | SPOKE held-floor\n' "$(date -Ins)" "$LABEL" "<i>" >> ./voicetest_$LABEL.log
   d. To hand the mic to the next waiting session between rounds, pass
      end_burst=true on the next converse call's pause, then continue.
4. If I say "stop" or "done", stop immediately.
5. After 4 rounds append:
   printf '%s | %s | %s\n' "$(date -Ins)" "$LABEL" "FINISHED" >> ./voicetest_$LABEL.log
   and tell me "session <LABEL> done".

You do NOT need me to answer each round — if the listen window times out on silence,
treat it as "continue". Keep the voice loop alive per the project rules; only stop
on "stop"/"done".
```

Suggested voices: A=`af_sky` (US F), B=`am_puck` (US M), C=`bf_emma` (UK F) — easy
to tell apart by ear.

## Observe (from a 4th shell or this repo)

```bash
cat ~/.voicemode/logs/ensure.log     # proxy auto-start trace
./voicemode-switch queue             # floor holder + waiters
./voicemode-switch queue-log         # live event stream (acquired_floor / queued / burst_yield)
./voicemode-switch health            # proxies up?
```

## Success criteria

- You **hear one voice at a time**, never two overlapping.
- `queue` shows one holder + the others QUEUED, rotating FIFO; `queue-log` shows
  `acquired_floor` / `queued` / `burst_yield (pause_max_hold)`.
- Merged timeline shows **non-overlapping** SPOKE windows in arrival order:
  ```bash
  sort voicetest_*.log
  ```
- Cross-check audio windows in the event log (per turn, non-overlapping):
  ```bash
  grep -E "TTS_START|TTS_PLAYBACK_END|RECORDING_(START|END)" \
    ~/.voicemode/logs/events/voicemode_events_$(date +%F).jsonl | tail -30
  ```

## Notes / known behavior

- TTS generation should be ~5s (first request per voice ~12s cold-start). If it's
  tens of seconds, the routing/`voicemode.env` config is wrong — run
  `./voicemode-switch test-tts`.
- Without the listen cap, a silent holder holds ~15–40s/turn (full listen window);
  with the cap (default 8s) rotation roughly halves. Tune with
  `VOICEMODE_QUEUE_LISTEN_CAP`.
- Clean up artifacts afterward: `rm -f voicetest_*.log`.
