# The ~61-second STT stall

**Status:** mechanism validated, trigger still unknown. Mitigation shipped;
instrumentation live and waiting to catch one.

**First observed:** 2026-09-06. **Investigated:** 2026-09-06.

## Symptom

Every so often a voice turn goes silent for just over a minute, then continues
normally. No error is ever shown — the transcript comes back correct.

## What is validated

All figures below were derived in-session from
`~/.voicemode/logs/events/voicemode_events_*.jsonl` (`STT_COMPLETE` →
`data.metrics.request_time_ms`), n = 5,687 transcriptions across all days.

| Fact | Evidence |
|---|---|
| Stalls are 61.1–64.4s, never in between | 9 occurrences on 2026-09-06 |
| Rate on the day: 8 of 36 calls = 22% | same file, same day |
| Zero occurrences before that day | 5,651 prior calls; slowest non-stall ever 30.6s, and those scale with a 10–13MB file |
| The duration decomposes exactly | `60.0` client timeout + `0.5` backoff + the real transcription time. A 417KB stall took 61.5s; a 749KB one took 64.4s |

The mechanism is in `voice_mode/simple_failover.py`:

- `:413` `timeout=60.0` on the STT `AsyncOpenAI` client (all endpoints, hardcoded)
- `max_retries=0` for local, with the VM-926 backoff loop below it doing the retrying
- `STT_RETRY_ATTEMPTS=2`, `STT_RETRY_BACKOFF=0.5` (`config.py:809-811`)
- `_is_transient_stt_error` classes `APITimeoutError` as transient

So: attempt 1 hangs → killed at exactly 60s → 0.5s backoff → attempt 2 succeeds
in ~1s. The user never sees an error, only the silence.

## What is ruled out — by measurement, not reasoning

| Hypothesis | How it died |
|---|---|
| Whisper or the proxy is slow/wedged | 25 sequential transcriptions through the proxy: 0 stalls, median 0.72s. 150 more through the identical `AsyncOpenAI` client path (same construction, same timeout, same never-closed client): 0 stalls. 175 clean calls outside the MCP process. |
| Concurrent sessions colliding | Zero overlapping STT windows for all 9 stalls — and zero overlapping TTS too. Upstream's conch serialises sessions, so cross-session concurrency effectively does not exist. |
| Audio length | Stalls happen on the *smallest* files (120–749KB) while 6MB files transcribe in 6.6s. |
| A stale pooled connection after idle | Stalls occur after 27s idle and after 272s idle; a 2,094s idle gap transcribed in 0.9s. No correlation. |
| Single-threaded proxy serialising clients | The proxy became `ThreadingHTTPServer` in `ab5e365` and was restarted at 11:27 UTC. Six of the nine stalls happened after that. |
| Event-loop starvation (e.g. a WSLg audio block) | The 60s timeout is enforced *by* the event loop. It fired at 60.0s to the tenth, nine times out of nine. A starved loop could not be punctual. |
| PTT key presses | The only presses on the day were at 13:13/13:21/13:22 UTC; the stalls cluster elsewhere. |

## Where the hung attempt goes: nowhere (validated, n=9/9)

The whisper container's own uvicorn access log settles it. For each of the nine
stall windows, count the `POST /asr` lines whisper logged between `STT_START`
and `STT_COMPLETE`:

| stall window (UTC) | duration | whisper POSTs inside it |
|---|---|---|
| 11:20:03–11:21:04 | 61.5s | 1, at +61.5s |
| 11:23:52–11:24:54 | 61.2s | 1, at +61.2s |
| 12:44:41–12:45:43 | 61.1s | 1, at +61.1s |
| 12:55:30–12:56:32 | 62.0s | 1, at +62.0s |
| 13:04:56–13:05:58 | 61.2s | 1, at +61.2s |
| 13:27:08–13:28:09 | 61.2s | 1, at +61.2s |
| 13:59:44–14:00:48 | 64.4s | 1, at +64.4s |
| 14:08:53–14:09:55 | 62.3s | 1, at +62.3s |
| 14:20:16–14:21:18 | 61.4s | 1, at +61.4s |

Nine out of nine: **exactly one** request, and it lands at the very end. That one
is the retry. For the whole first sixty seconds whisper hears nothing at all.

So the hang is upstream of whisper, and whisper is exonerated. Two candidates
remain, and the proxy tracing (added after the last of these stalls) tells them
apart on the next occurrence:

- the request never reached the proxy — the failure is inside voice-mode's HTTP
  client, at connect or write; or
- it reached the proxy and the proxy did not forward it inside 60s. The only
  60s-scale blocking path before the forward is `self.rfile.read(content_length)`
  waiting on a request body the client never finished sending — which points back
  at the client anyway.

A third possibility is already excluded: had the proxy forwarded and *waited*,
its own upstream timeout is 30s and it would have returned 502 at ~30s. `502` is
`>= 500`, so `_is_transient_stt_error` would have retried it and the total would
be ~31s, not 61s.

*(Care with this log: the `language=auto` 500s at 14:23 UTC are an artefact of a
malformed direct-to-whisper arm in this session's own hammer test, not a real
failure. They are excluded above.)*

## Mitigation (shipped)

`patches/patch_simple_failover.py` gained a second, independent edit replacing
the hardcoded `timeout=60.0` with:

```
VOICEMODE_STT_TIMEOUT        seconds, all STT endpoints  (default 60 — unchanged)
VOICEMODE_STT_TIMEOUT_LOCAL  seconds, local endpoints only (default: as above)
```

Defaults are unchanged, so the patch is a no-op until an operator opts in. This
machine sets `VOICEMODE_STT_TIMEOUT_LOCAL=15` in `~/.voicemode/voicemode.env`,
which turns a 61s stall into ~16s and caps the pathological all-three-attempts
case at ~46s instead of ~181s.

**Sizing it is a measurement, not a preference.** Local STT time scales with
audio length:

| audio | GPU whisper `small` (since 2026-08-25) | CPU whisper (historical) |
|---|---|---|
| typical turn | median 1.1s, p95 3.6s, max 6.6s (n=67) | median 1.5s at <1MB |
| 10MB recording | not observed | 24–29s |

15s is ~2x headroom on GPU. **Raise it back to 60 before switching
`voicemode-switch compute cpu`.**

## Instrumentation now in place

1. **`whisper-proxy.py` per-request tracing** — `ACCEPT` / `BODY` /
   `UPSTREAM_OK|ERR` / `DONE` with millisecond deltas, to
   `~/.voicemode/logs/whisper-proxy.log`. Note the proxy's own upstream timeout
   is 30s, so *if a stalled attempt ever reaches the proxy it cannot take 60s* —
   the absence of an `ACCEPT` during a stall is itself the finding.
2. **`log_message` now flushes.** It never did, so the access log sat at 0 bytes
   all day behind a block-buffered stdout redirect. That is why there was no
   server-side evidence to begin with.
3. **`VOICEMODE_DEBUG=true`** in `voicemode.env`, so `simple_failover` logs
   `STT transient failure on <url> (try 1/3): <exception>` to
   `~/.voicemode/logs/debug/`. The exception *class* is the decisive fact:
   `APIConnectionError` = the request never left the process;
   `APITimeoutError` = it left and nothing came back.
4. **A TCP sampler on port 2022** (5 Hz, `ss -tnpo`) to distinguish "no socket
   was ever opened" from "socket open, bytes stuck in Send-Q" from "request
   delivered, response lost".

Each of these takes effect on a fresh voice-mode process (`/mcp` → reconnect).

## Open lead

The stall reproduces only inside the live voice-mode process, never in 175
isolated calls of the same code. Something about the running session is
involved, and the only things new on the day it started are the upstream-8.12
realignment and the PTT control channel. One WSL-specific patch,
`patch_audio_keepalive.py`, was deleted that day — the audit's reasoning for
removing it is sound (upstream's playback wait became an async poll loop), and
the punctual 60.0s timeout argues against loop starvation, so it is a suspect
and not a cause. Restoring it is a cheap experiment if the traces come back
empty.
