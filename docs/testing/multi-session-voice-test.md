# Multi-session voice queue — manual test

Reproducible test for the cross-session voice queue: proxy auto-start, multi-voice
routing, strict FIFO turn-taking, no talk-over (barge-in fix), and the
no-speech timeout under contention.

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
5. **No-speech timeout** — under contention a SILENT holder yields in
   ~`VOICEMODE_QUEUE_LISTEN_CAP` (8s) instead of the full window. A user who
   IS speaking is never cut off (the timeout only fires before speech starts).

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

## Self-executed (hands-off) variant — agent launches the sessions

Instead of you opening 3 terminals, the agent can launch 3 **headless** Claude
sessions that drive the voice loop autonomously (silence → continue), so the
whole test runs and self-reports with no human in the loop. Validated on
voice-mode 8.7.1 (2026-06-10): clean FIFO rotation, non-overlapping speech.

**1. Readiness** (proxies up + expected version):
```bash
./voicemode-switch ensure && ./voicemode-switch health
.venv/bin/python -c "import importlib.metadata as m; print(m.version('voice-mode'))"
rm -f voicetest_*.log
```

**2. Write one prompt file per session** (`/tmp/vmtest_<L>.txt`), substituting
LABEL + VOICE. Key: instruct N rounds of `mcp__voicemode__converse`
(`wait_for_response=true`, short `listen_duration_max`), the QUEUED-retry contract,
"silence/timeout = continue", and `printf '%s | <L> | ...' "$(date -Ins)" >> ./voicetest_<L>.log`
START / per-round SPOKE / FINISHED lines. (Template: this repo's git history of
`/tmp/vmtest_A.txt`, or reconstruct from the paste-prompt above.)

**3. Launch headless, staggered** (each in the background, from the repo dir):
```bash
claude -p "$(cat /tmp/vmtest_A.txt)" --dangerously-skip-permissions &   # voice af_sky
sleep 3
claude -p "$(cat /tmp/vmtest_B.txt)" --dangerously-skip-permissions &   # voice am_puck
sleep 3
claude -p "$(cat /tmp/vmtest_C.txt)" --dangerously-skip-permissions &   # voice bf_emma
```
Notes: `-p` (print mode) runs the prompt agentically to completion then exits —
no `--max-turns` needed. `--dangerously-skip-permissions` avoids prompts. Use
`"$(cat file)"` so literal `$(date -Ins)` inside the prompt is NOT expanded by
the launching shell (it reaches Claude verbatim, run by Claude's Bash tool).
Stagger by a few seconds so they arrive in a defined order.

**4. Monitor & verify** (the agent polls these):
```bash
./voicemode-switch queue          # one holder + waiters, rotating FIFO
sort voicetest_*.log              # non-overlapping SPOKE windows in arrival order
pgrep -af "claude -p" | wc -l     # sessions still running (0 when all finished)
```
PASS = sequential (non-overlapping) SPOKE timestamps across the 3 files, queue
shows FIFO rotation, sessions exit cleanly.

**5. Clean up:**
```bash
pkill -f "claude -p" 2>/dev/null; rm -f voicetest_*.log /tmp/vmtest_*.txt
```

## Notes / known behavior

- TTS generation should be ~5s (first request per voice ~12s cold-start). If it's
  tens of seconds, the routing/`voicemode.env` config is wrong — run
  `./voicemode-switch test-tts`.
- Without the no-speech timeout, a silent holder holds ~15–40s/turn (full listen
  window); with it (default 8s) rotation roughly halves. Tune with
  `VOICEMODE_QUEUE_LISTEN_CAP`. Active speech is never truncated by it.
- Clean up artifacts afterward: `rm -f voicetest_*.log`.
