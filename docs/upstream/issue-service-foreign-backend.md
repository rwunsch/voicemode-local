# Draft: service management fights a backend it didn't start

**Type:** bug report. **Target:** `mbailey/voicemode` new issue. **Status:** not submitted.

> **This is where the "add a Docker endpoint" idea actually belongs.** The endpoint already
> exists — `VOICEMODE_TTS_BASE_URLS` / `VOICEMODE_STT_BASE_URLS` accept any URL, and 8.11's
> release notes already say "works with local or remote OpenAI-compatible STT/TTS
> endpoints". Pointing voice-mode at a Docker container needs no code at all.
>
> What is missing is one layer down: `service.py` assumes it *owns* every backend, so it
> reports a perfectly healthy foreign backend as "not running" and will start a second one
> on top of it. Proposing "add Docker support" would be rejected as a philosophy change.
> Proposing "don't fight a backend you didn't start" is a bug report with a body count.

---

## Issue body

**Title:** `service start/status ignores a healthy backend it didn't start, then collides with it`

`voicemode service {status,start}` detects services only via its own systemd/launchd units
and their PIDs. If the configured endpoint is already served by something voice-mode didn't
launch — a Docker container, a manually started `kokoro-fastapi`, a remote host — then:

- `voicemode service status kokoro` reports **not running**, while TTS is working fine.
- `voicemode service start kokoro` starts a **second** server on the same port.
- The loser of the port race crash-loops. Under `Restart=always` (VM-1398) it retries forever.

**Observed.** A Docker Kokoro on `:8880` alongside an enabled `voicemode-kokoro.service`
produced **16,174 failed unit starts over 14 days** on one machine, burning CPU continuously
and intermittently taking down TTS when the container lost the race after a host reboot.
`systemctl --user stop` was not enough — the unit revived on every restart until `disable`.

**Why this is easy to hit.** Nothing warns you. The env var says local TTS is at
`http://127.0.0.1:8880/v1`; both a container and the managed unit satisfy that URL; and
`service enable` is offered during install without checking whether the port is already
answering.

**Reproduce.**
```bash
docker run -d -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu
voicemode service status kokoro       # -> "not running"  (but curl :8880 works)
voicemode service start kokoro        # -> starts a second server; one crash-loops
```

**Suggested fix, cheapest first.**

1. **Probe the port before claiming a service is down.** If `service status` finds the
   configured base URL answering `/v1/audio/voices` (TTS) or `/v1/models` (STT), report
   something like `running (not managed by voicemode)` rather than `not running`. This is
   a few lines in `check_service_status` and fixes the misleading output on its own.
2. **Refuse to start over a live port.** `service start` should fail with "port 8880 is
   already serving — voicemode did not start it" instead of racing.
3. **Warn at `service enable` / install time** if the port is already answering.
4. **Docs:** one short note that running your own backend (Docker, remote, manual) is
   supported via `VOICEMODE_*_BASE_URLS` and that you should *not* also enable the managed
   unit for that service.

(1) and (4) alone would have prevented the failure above, and neither requires voice-mode
to know what Docker is — which seems right, given the project deliberately manages
lifecycles itself via systemd/launchd.

I'm happy to send a PR for (1) and (2) if that shape is welcome.
