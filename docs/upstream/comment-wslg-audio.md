# Draft comments: WSLg audio issues #341 / #342

**Target:** existing open issues. **Status:** not submitted. **Comment first, PR only if a maintainer engages.**

- **#341** "Audio cuts off at ~19 seconds on WSLg / Docker Desktop" (open since 2026-04-16)
- **#342** "Mild crackling on WSLg after extended use" (open since 2026-04-16)

We've chased WSLg audio failures for months and found **six distinct causes** wearing one
symptom. Posting the taxonomy rather than a patch, because the single most useful thing here
is that "stutter on WSL" is not one bug — a fix for one of these does nothing for the others,
which is probably why this issue has stayed open.

> **Before posting:** re-verify each item against 8.12.0. Several were diagnosed against
> 8.7.1 and 8.11/8.12 have moved. Only post what still reproduces. Mark clearly which we
> measured and which is inference.

---

## Comment for #341 (cut-off)

Two of our causes produce a hard cut rather than a stutter:

**(a) A hard cap at `listen_duration_max`.** `converse.py:1467` bounds the recording loop by
`recording_duration < max_duration` regardless of whether speech is in progress. Recordings
end at exactly the cap with the transcript stopping mid-word. Not WSL-specific, but WSL makes
it likelier by inflating latency. Filed separately — see <ISSUE>.

**(b) An orphaned playback stream.** On WSL, output is wired through WSLg's `module-rdp-sink`,
which buffers ~1s+. If a `converse` is mid-playback when the MCP server is shut down (reconnect,
config reload, cancelled call), the PortAudio output stream's host thread keeps the interpreter
alive after `mcp.run()` returns — the old process does not die. The next session starts a
second voice-mode, and two processes now hold sink-inputs on the same RDP sink. Audible result:
mixing, stutter, and stale trailing audio from the previous session.

Diagnostic: `pactl list sink-inputs` while it happens, and `pgrep -af voice-mode` — more than
one live process is the tell.

8.12.0's `mcp_shutdown_patch.py` (VM-2015) restores transport-close cancellation of in-flight
handlers, which is adjacent but not this: `grep -rn 'os._exit' voice_mode/` returns nothing, so
nothing forces exit when a lingering audio thread holds the interpreter open. We carry a small
patch that forces termination on shutdown once playback teardown has been given its chance.
Happy to send it if useful.

## Comment for #342 (crackling / stutter)

Three further causes, none of which is a voice-mode bug — worth documenting so people stop
looking for one:

**(c) Streaming PCM playback has no jitter buffer.** Chunks are written as they arrive, so any
scheduling hiccup is an immediate underrun — "a few ms of dropout every second". The
discriminating test: `paplay` a pure tone through the same sink. If the tone is clean while
voice-mode stutters, WSLg is healthy and the problem is upstream of it. Workaround that fixed
it for us: `VOICEMODE_STREAMING_ENABLED=false` (needs an MCP restart). A small jitter buffer on
the streaming path would fix it properly.

**(d) CPU oversubscription.** WSLg's audio transport thread cannot be pinned or prioritised from
inside the distro. Several concurrent agent sessions, each with an MCP fleet, took this machine
to load 13 on 12 cores and the transport starved — stutter that clears on its own as load drops.
Distinguishing feature: it is *self-clearing*, which sends people hunting for a race that isn't
there. `perf` cannot reach the WSLg distro, so this is measurable only by correlating load.

**(e) CPU-only Kokoro saturating the cores the audio pipeline needs.** Same shape as (d) but
self-inflicted by TTS. Capping Kokoro's CPUs (we use 6) or moving it to GPU removed it.

**(f) Two servers on one port.** Not WSL-specific, but it looks like an audio bug: a Docker
Kokoro and an enabled `voicemode-kokoro.service` both claiming `:8880`, the loser crash-looping
(16,174 failed starts in 14 days on one machine). Filed separately — see <ISSUE>.

**Suggested cheap wins for the project**, in order of value: the jitter buffer for (c); a
troubleshooting doc entry naming (d)/(e) so users stop bisecting voice-mode for a load problem;
the shutdown fix for (b).
