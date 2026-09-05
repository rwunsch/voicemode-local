# Kokoro :8880 Port Collision — Crash Loop

> **TL;DR:** Two Kokoro servers (the upstream `voicemode-kokoro.service` systemd user unit and this
> project's Docker container) both want host port `8880`. The loser crash-loops forever, burning CPU
> and periodically stealing/dropping the TTS endpoint. Resolved 2026-08-04 by **disabling** the
> systemd unit and letting the Docker/GPU container own 8880.

| Field        | Value                        |
|--------------|------------------------------|
| Status       | Complete                     |
| Created      | 2026-08-04                   |
| Last Updated | 2026-08-04                   |
| Project      | voicemode-local (WSL2)       |

## Why

Reported symptom: "Kokoro keeps crashing." Two distinct failures share this root cause:

1. **Voice flap** (first diagnosed 2026-07-17) — Kokoro voice names start 404'ing mid-session and
   voice falls back to the Piper voice set, because host `:8880` goes away while one instance
   reloads its model.
2. **Permanent crash loop** (diagnosed 2026-08-04) — the instance that loses the port race never
   gives up. `Restart=always` + `RestartSec=10`, and uvicorn binds the socket *after* application
   startup, so every 10 seconds the service loads the full Kokoro model, *then* dies on
   `[Errno 98] address already in use`.

Measured on 2026-08-04 01:35 CEST:

```bash
journalctl --user -u voicemode-kokoro.service --since '14 days ago' \
  | grep -c 'address already in use'
# 16174
```

Per-day: Jul 21 = 581 · Jul 22 = 2138 · Jul 23 = 1825 · Jul 24 = 752 · Jul 25 = 1508 ·
Jul 27 = 871 · Jul 29 = 1281 · Aug 01 = 1542 · Aug 02 = 3398 · Aug 03 = 2277. The systemd restart
counter had reached **3261**. One observed cycle logged `Consumed 5min 44.261s CPU time` — a
permanent background CPU heater, and a plausible feeder of the known
CPU-oversubscription audio stutter (see `docs/compute-modes/README.md`).

## What Was Done

Made the Docker container the single owner of `:8880`, consistent with `INSTALL_MODE=docker` and
`COMPUTE_MODE=gpu` in `~/.voicemode-local/config`:

```bash
systemctl --user disable --now voicemode-kokoro.service
docker restart voicemode-kokoro
```

The Docker container is also the *correct* owner on this machine: it reports `CUDA: True` and sees
the RTX 4080, while the native unit reports `Loading Kokoro model on cpu` / `CUDA: False` despite
running `start-gpu.sh`. Measured generation times for comparable chunks: **native 9.3 / 8.9 / 19.2 s**
vs **Docker 1.3 s**.

Not done: repairing the native unit's broken CUDA (its venv `torch 2.6.0+cu124` returns
`torch.cuda.is_available() == False`, and uv warns the venv interpreter is 3.10.15 vs the 3.10.20 it
was built with). Unnecessary once the unit is disabled — see `docs/` on venv breakage if the native
path is ever wanted again.

## How to Recreate (diagnosis)

The container being `Up` and `RestartCount=0` is **not** evidence that Kokoro is healthy — the
crash loop lives in systemd, not Docker.

```bash
# 1. Are there two owners?
systemctl --user list-units --all | grep -i kokoro     # native unit
docker ps --format '{{.Names}}\t{{.Ports}}' | grep kokoro

# 2. Who actually holds the host port?
ss -ltnp | grep 8880
#   line with users:(("uvicorn",...))  -> native systemd unit owns it
#   bare "*:8880" line, no user info   -> root-owned docker-proxy owns it

# 3. The decisive evidence — the crash loop
journalctl --user -u voicemode-kokoro.service --since '7 days ago' \
  | grep -E 'address already in use|Scheduled restart|Main process exited'
```

### Fix

```bash
systemctl --user disable --now voicemode-kokoro.service
docker restart voicemode-kokoro          # ~21 s until the host port answers
```

### Verification

```bash
systemctl --user is-enabled voicemode-kokoro.service   # disabled
systemctl --user is-active  voicemode-kokoro.service   # inactive
ls ~/.config/systemd/user/default.target.wants/ | grep kokoro   # must be EMPTY

curl -sf http://127.0.0.1:8880/health                   # answers
./voicemode-switch health                               # all three services up

# confirm the DOCKER instance is the one serving:
curl -s -o /dev/null -X POST http://127.0.0.1:8880/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"kokoro","input":"test","voice":"af_sky","response_format":"mp3"}'
docker logs voicemode-kokoro --since 1m | grep 'audio/speech'   # request appears here
```

## Gotchas

- **`stop` is not `disable`.** This exact fix was prescribed on 2026-07-17 but only the *stop* half
  took effect — the enablement symlink
  `~/.config/systemd/user/default.target.wants/voicemode-kokoro.service` still carried its original
  `Jul 2 13:06` mtime, so the next WSL restart revived the race (first bind failure: Jul 21). Always
  check `is-enabled` **and** the symlink, not just `is-active`.
- **Both things are named `voicemode-kokoro`** — the systemd unit and the Docker container. Say
  which one you mean.
- **The systemd unit exits `1`, not `0`.** The unit file's comment explains `Restart=always` in terms
  of uvicorn's clean exit-0 on `UVICORN_LIMIT_MAX_REQUESTS`. That's a different code path; a bind
  failure is exit 1, and `Restart=always` turns it into an infinite loop rather than a dead unit.
- **`ss -ltnp` without sudo hides root-owned listeners' process info.** A published Docker port shows
  as a bare `*:PORT` line with no `users:(...)` field — that absence is the signal, not an error.
- **Container IPs aren't routable from the WSL distro** under Docker Desktop. To test the container
  directly, use `docker exec voicemode-kokoro curl http://127.0.0.1:8880/...`, not its `172.x` address.
- `voicemode-switch ensure` is already collision-safe — it starts Docker only when 8880 isn't
  answering — so nothing in this repo re-creates the race once the unit is disabled.

## Configuration

| Where | Key | Value |
|---|---|---|
| `~/.voicemode-local/config` | `INSTALL_MODE` | `docker` |
| `~/.voicemode-local/config` | `COMPUTE_MODE` | `gpu` |
| `~/.config/systemd/user/voicemode-kokoro.service` | — | disabled 2026-08-04; leave disabled |

If the native unit is ever wanted as the owner, stop and disable the Docker container instead — but
fix its CUDA first, or you are choosing the CPU-only path.
