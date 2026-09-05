# Patch audit: voicemode-local patches vs voice-mode 8.12.0

**Baseline:** voice-mode 8.12.0, clean venv (`uv venv --python /usr/bin/python3.12`), no patches applied.
**Date:** 2026-09-05. **Live install at audit time:** voice-mode 8.7.1.

Method: two independent checks per patch. (1) Run the patcher against a scratch copy —
each patcher is its own drift detector and exits 1 naming the anchor. (2) A behaviour
check that asks whether the *bug* is fixed, since an anchor can drift while the defect
survives, and an anchor can survive while the defect is fixed elsewhere.

Every verdict below cites a command or a line number. Changelog text is quoted only as
corroboration, never as the sole basis for a verdict.

## Drift magnitude, 8.7.1 → 8.12.0

| File | 8.7.1 | 8.12.0 | diff lines |
|---|---|---|---|
| `tools/converse.py` | 2261 | **4620** | 3488 |
| `core.py` | 831 | 1088 | 463 |
| `simple_failover.py` | 363 | 562 | 390 |
| `server.py` | 109 | 106 | 36 |

`converse.py` doubled. VM-1961 split `converse()` into a thin `@mcp.tool()` wrapper plus
`_converse()`, and 8.11 added `turns[]`, surveys, and the control channel.

## Verdicts

| Patch | Anchors | Behaviour check | Verdict |
|---|---|---|---|
| `patch_converse_cancel.py` | DRIFT (`marker 'start'` matched 3×) | `test_vm2015_cancel.py` PASS — no `CancelledError` handler returns without re-raising. Upstream's own comment at `converse.py:4205`: *"VM-2015: this used to swallow the CancelledError and `return` a…"* | **OBSOLETE** |
| `patch_listen_stall.py` | DRIFT (`'loop-head'` 0×) | `test_811_stall_backstop.py::test_recording_loop_has_a_wallclock_backstop` PASS. `converse.py:1465-1468` — `AUDIO_STALL_TIMEOUT = 5.0`, `last_audio_time = time.monotonic()`, bumped on every chunk at :1521. Same fix as ours. | **OBSOLETE** |
| `patch_listen_overrun.py` | DRIFT (`recording-loop anchor` 0×) | **FAIL** — `converse.py:1467` is still `while (recording_duration < max_duration and …)`. Upstream comment at :1462 confirms deliberately: *"This is a dead-stream safety net, NOT a cap on recording length … length is still governed by `recording_duration < max_duration`."* And :1559: *"The only exit is speech detection or max_duration."* | **STILL NEEDED — live upstream bug, PR candidate** |
| `fcntl_shim.py`, `resource_shim.py` | n/a (file copies) | `grep '^import fcntl\|^import resource'` across the package → **0 hits** outside `file_lock.py`, which guards properly (`file_lock.py:21` `if sys.platform != "win32": import fcntl`, `:34` `import msvcrt`). Upstream's is the better implementation — msvcrt region lock at a high offset, SQLite's locking-byte trick. | **OBSOLETE** |
| `voice_queue.py` + `patch_converse_queue.py` | DRIFT (`anchor 'import'` 0×) | Superseded by 8.8.0 epic VM-1610: `conch_queue.py` (696 lines), `conch_ops.py`, `conch_notify.py`, `cli_commands/conch.py`, `tools/conch.py`, 6 test files. Same design; better ordering (flock'd `conch.queue.seq` vs our `epoch-µs`, so correct across machines); plus callback mode, notify-on-give, fair promotion, MCP parity, CLI — none of which we built. | **SUPERSEDED — delete** |
| `patch_shutdown_abort.py` | **OK** (applied clean) | 8.12.0 ships `mcp_shutdown_patch.py`, but it solves a *different* problem — restoring transport-close cancellation that fastmcp's `LowLevelServer.run()` drops. `grep 'os._exit\|_exit('` across `server.py`, `mcp_shutdown_patch.py`, `core.py` → **0 hits**. Nothing force-exits when a lingering PortAudio thread keeps the interpreter alive. Our orphan-stream failure is unaddressed. | **STILL NEEDED** |
| `patch_audio_keepalive.py` | DRIFT (`expected 1 match, got 0`) | Structure changed materially: `_wait_for_player_with_control` (`core.py:41`) is now an **async poll loop** (`await asyncio.sleep(_CONTROL_POLL_INTERVAL)`), not a blocking executor wait. The event loop is no longer starved during playback, which was the whole premise of this patch. `player.wait()` at `:68` still blocks, but only at teardown. Our patch existed to service *our* queue's heartbeat — which Task 4 deletes. | **LIKELY MOOT — re-verify after the conch migration** |
| `patch_simple_failover.py` | **OK** (applied clean) | **Not fixed.** `simple_failover.py:84-97` still carries `openai_voices = [...]` and `voice_mapping = {"af_sky": "nova", "af_sarah": "nova", …}` with `selected_voice = voice_mapping.get(voice, "alloy")`. A Kokoro/Piper outage still silently swaps the user to a cloud voice mid-conversation. VM-1556 (8.8.0) fixed the *default config* falling back to OpenAI — a different code path. | **STILL NEEDED — live upstream behaviour, PR candidate** |

## Summary

- **Retire 5 artifacts:** `patch_converse_cancel.py`, `patch_listen_stall.py`, `fcntl_shim.py`, `resource_shim.py`, and (as superseded) `voice_queue.py` + `patch_converse_queue.py`.
- **Keep 3:** `patch_listen_overrun.py`, `patch_shutdown_abort.py`, `patch_simple_failover.py`.
- **Re-verify 1 after migration:** `patch_audio_keepalive.py`.
- **Net:** the patch set shrinks from ~1,400 lines to roughly 250, and two of the three survivors are genuine upstream defects with clean, isolated fixes — i.e. good PR material.

## Open question raised during the audit (not yet measured)

Upstream's conch hold TTL is **10s, refreshed** (`conch.py:62`, VM-1649). A single TTS
utterance frequently plays longer than 10s. If nothing bumps the hold during playback,
a long reply could lapse the floor mid-speech and let a waiting session cut in — the
same class of failure our `patch_audio_keepalive.py` was written for, relocated into
upstream's model. **Must be tested during Task 4 before we trust conch for the user's
multi-session workflow.** If it reproduces, it is a third PR candidate.

## Addendum — verdicts re-verified against pristine source (2026-09-05)

A fresh venv unexpectedly reported "already patched", which exposed a hazard (below) and
put the audit's own baseline in doubt. Both live-defect verdicts were therefore re-checked
against source fetched **directly from `mbailey/voicemode` master via the GitHub API** — no
venv, no `uv`, no cache. Our marker count in the fetched files: **0**.

| Verdict | Pristine-source evidence |
|---|---|
| `patch_listen_overrun` STILL NEEDED | `converse.py:1467` — `while (recording_duration < max_duration and not stop_recording` |
| `patch_simple_failover` STILL NEEDED | `simple_failover.py:89` — `"af_sky": "nova",` and `:97` — `selected_voice = voice_mapping.get(voice, "alloy")` |

Both hold. (master HEAD is the 8.12.0 version bump, so master == 8.12.0 for these files.)

## Hazard found: `uv` hardlinks make in-place patching write through to the cache

**Validated, end to end.** `uv pip install` populates a venv by **hardlinking** from
`~/.cache/uv`, so patching a file under `site-packages` mutates the shared inode — the cache
entry and every other venv built from it.

Measured: four test venvs sharing **inode 14004386, links=5**; the fifth link is
`~/.cache/uv/archive-v0/fFRJWaesm5jyhXzG/voice_mode/tools/converse.py`. After patching one
venv, that cache entry carried our patch marker, and the decisive test — building a brand-new
venv — produced `marker=1` on a supposedly clean install.

**Consequences.** A "clean rebuild" is not clean; a future rebase could start from
half-patched bytes; and `apply.sh`'s idempotence guard would silently skip work it should do.

**Fix (committed).** `apply.sh` now replaces each patch target with a private copy before
writing (`unshare_file`), so patching is local to the venv. Verified in isolation: the
cache-side file stays unmodified while the venv copy is patched, and the helper no-ops when
link count is already 1. It is also a no-op under `pip`, which copies rather than links.

**Outstanding.** The already-poisoned entry `fFRJWaesm5jyhXzG` is still present —
`uv cache clean voice-mode` failed with a 300s lock timeout (a concurrent `uv` process held
`~/.cache/uv/.lock`). **Must be cleaned before the Task 8 cutover**, or the live install will
be built from contaminated bytes.
