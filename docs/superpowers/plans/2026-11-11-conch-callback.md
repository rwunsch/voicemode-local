# Real voice-queue callback on every harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A session that queued with `conch_mode="callback"` is woken when its turn comes, a busy or silent one never blocks anybody, and a session that is present always gets the mic over one that has not come back.

**Architecture:** The policy lives in our copied module `patches/conch_wakers.py` (wake counting, "absent" test, result codes). Small anchor-checked insertions in upstream's `conch_queue.py` and `conch.py` (via the existing `patches/patch_conch_wakers.py`) call it. Upstream's queue order, wait mode, hold and names stay untouched.

**Tech Stack:** Python 3.10+ stdlib; pytest; upstream voice-mode 8.12.0 pristine sources via `VM812_SRC` (a flat folder of the upstream files, `tools/converse.py` as `converse.py`).

**Spec:** `docs/superpowers/specs/2026-11-11-conch-callback-design.md`

## Global Constraints

- Patches: anchor-checked against pristine 8.12.0 (exactly one match, else exit 1 with "voice-mode has changed"), idempotent via the marker `VML: CONCH WAKERS`, CRLF-safe.
- Every new branch fails safe to upstream behaviour: wrap in `try/except Exception`, never raise into a grant site or `try_acquire`.
- Both manifests (`patches/manifest.json`, `patches/manifest.windows.json`) list the same conch steps; `conch_queue.py` and `conch.py` join `unshare` in both.
- Settings: `VOICEMODE_CONCH_CALLBACK_GRACE` default `10` (s); `VOICEMODE_CONCH_MAX_WAKES` default `5` (per queued turn); given grants hold for upstream's `CONCH_GRANT_TTL` (default 30 s).
- `VOICEMODE_CONCH_WAKE_COMMAND` is read from the environment only. Exit 0 = delivered; exit 75 = busy/permission prompt (refused, try again next grant event); anything else = cannot reach.
- `session_wake.py` rules unchanged except the `conch` message text.

## Review Focus

1. A woken session that never comes back while a second session queues in wait mode: the second must get the mic after the 10 s grace, and the first keeps its `seq`.
2. A session with `wait_for_conch=false` arriving while a lone, absent callback grant stands: it must be served after the grace, not told "unknown holds the voice channel".
3. A `conch give` to a callback session: it holds against present callers for 30 s, then yields.
4. The sixth grant event for the same queued turn: no wake is sent.
5. A grant file written by unpatched upstream (no `given`, no `granted_at`): treated as not given; with no `granted_at`, it is never "absent" (no guessing), exactly as upstream treats it.

---

### Task 1: Policy in `conch_wakers.py` (wake cap, absent test, exit 75, message)

**Files:**
- Modify: `patches/conch_wakers.py`
- Modify: `patches/session_wake.py` (`MESSAGES["conch"]`)
- Test: `tests/test_conch_wakers.py`

**Interfaces:**
- Produces:
  - `GRACE_SETTING = "VOICEMODE_CONCH_CALLBACK_GRACE"`, `MAX_WAKES_SETTING = "VOICEMODE_CONCH_MAX_WAKES"`, `EXIT_BUSY = 75`
  - `grace_s() -> float` (default 10.0; bad value → 10.0), `max_wakes() -> int` (default 5; bad value → 5)
  - `grant_is_absent(grant: dict, entry, now: datetime | None = None, ttl: float = 30.0) -> bool`: True iff `entry.mode == "callback"`, `grant["granted_at"]` parses, age > `grace_s()`, and (not `grant.get("given")` or age > `ttl`).
  - `take_wake(state_dir: Path, session_id: str, seq: int) -> bool`: increments the count for `(session_id, seq)` in `state_dir / "conch.wakes.json"` and returns True if the count is now ≤ `max_wakes()`. Drops entries of other seqs for that session. Never raises (an unreadable file starts at 0).
  - `deliver(entry, state_dir: Path | None = None) -> bool`: as today, but first calls `take_wake` when `state_dir` is given (cap reached → False, nothing sent). A command exiting `EXIT_BUSY` returns False without trying the other wakers.
  - `deliver_then(entry, fallback, state_dir: Path | None = None) -> None`: as today, passing `state_dir` to `deliver`.

- [ ] **Step 1: Write the failing tests** in `tests/test_conch_wakers.py`:
  - `test_grant_is_absent_after_the_grace_only_for_callback`: entry mode callback, `granted_at` 11 s ago → True; 9 s ago → False; mode wait 60 s ago → False.
  - `test_a_given_grant_holds_until_the_ttl`: `given: True`, 20 s → False; 31 s → True.
  - `test_a_grant_without_granted_at_is_never_absent`.
  - `test_grace_setting` (`"4"` → 4.0, `"x"` → 10.0) and `test_max_wakes_setting` (`"2"` → 2, `"0"` → 0, `"x"` → 5).
  - `test_take_wake_caps_per_queued_turn`: five True, the sixth False; a new seq for the same session starts again at True.
  - `test_deliver_stops_at_the_cap(tmp_path)`: with `max_wakes()` = 1, the second `deliver(entry, tmp_path)` sends nothing (`sw.wake` not called).
  - `test_a_busy_command_does_not_fall_through`: command exits 75 → `deliver` False and `sw.wake` not called.
  - `test_the_conch_message_lets_the_session_stay_silent`: `MESSAGES["conch"]` contains "nothing" and "converse", ≤ `MAX_MESSAGE_BYTES`, no newline.

- [ ] **Step 2: Run them, expect failures**

Run: `.venv/bin/python -m pytest tests/test_conch_wakers.py -q`
Expected: the new tests FAIL (names not defined), the old ones pass.

- [ ] **Step 3: Implement** the Interfaces above in `patches/conch_wakers.py`. Reword `MESSAGES["conch"]` in `patches/session_wake.py` to exactly:
  `'Voice queue: your turn on the voice channel has come. If you still have something to say, call the voicemode converse tool now (your queued message was not spoken). If not, do nothing: you keep your place.'`

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_conch_wakers.py tests/test_session_wake.py -q`. Expected: all pass.

- [ ] **Step 5: Commit** `git commit -m "feat(conch): wake cap per queued turn, absent-grant test, busy exit code"`

### Task 2: Upstream insertions (ping the lone grantee, given marker, present beats absent)

**Files:**
- Modify: `patches/patch_conch_wakers.py` (new anchors for `conch_queue.py` and `conch.py`; keep the converse and conch_notify ones)
- Modify: `patches/manifest.json`, `patches/manifest.windows.json` (targets of the `patch_conch_wakers.py` step become `["tools/converse.py", "conch_notify.py", "conch_queue.py", "conch.py"]`; `unshare` gains `"conch_queue.py"`, `"conch.py"`)
- Modify: `tests/test_native_manifest.py`, `tests/test_apply_py.py` (the registered intended step line and targets), and the `SOURCES` dicts in `tests/test_patch_trees.py`, `tests/test_apply_py.py`, `tests/test_apply_windows.py`, `tests/test_ptt_instant_patch.py`, `tests/test_native_manifest.py`, plus the two copy loops in `tests/run_windows_transport_tests.sh` (add `conch_queue.py conch.py`)
- Test: `tests/test_conch_wakers.py` (patch section)

**Interfaces:**
- Consumes: `conch_wakers.grant_is_absent`, `conch_wakers.deliver(entry, state_dir)` from Task 1.
- Produces, inside upstream (pristine 8.12.0 anchors):
  1. `ConchQueue.grant_next`: after its `_atomic_write_json(cls._grant_file(), {...})`, if `target.mode == "callback"`, call `cls._notify_callback(target, block=notify_block)`. Anchor: the line `        return target` that ends `grant_next` (check it is unique in context; anchor on the preceding `"granted_at": datetime.now().isoformat(),\n            },\n        )\n        return target\n` block).
  2. `ConchQueue.grant`: the written dict gains `"given": True`.
  3. `ConchQueue._current_grant`: in the `if e.session_id == sid:` branch, before `return g`: if `conch_wakers.grant_is_absent(g, e, ttl=_get_grant_ttl())` and a live waiter with `mode == "wait"` exists, write the grant to the first such waiter (same dict shape as `grant_next`, fresh `granted_at`, no `given`), ping `e` again with `cls._notify_callback(e, block=False)`, and return the new grant. The callback waiter is **not** deregistered.
  4. `Conch._queue_grant_blocks`: returns False when the grant names a live callback waiter and `grant_is_absent` holds (nobody present waits in wait mode, so a present caller may take the free mic).
  5. `conch_notify.notify_granted` passes `state_dir=Conch.LOCK_FILE.parent` into `deliver` / `deliver_then` (resolved lazily inside the try).

- [ ] **Step 1: Write the failing tests** (each applies the patch to a pristine copy under `tmp_path`, loads `conch.py`, `conch_queue.py`, `conch_notify.py` as a package `voice_mode` with the real `conch_wakers.py`, points `Conch.LOCK_FILE` into `tmp_path`, and stubs `session_wake.wake` to record calls):
  - `test_a_lone_callback_grantee_is_pinged_on_grant`: register A (callback); `grant_next()` → one wake for A.
  - `test_a_late_wait_waiter_passes_an_absent_callback_grant`: A callback granted 11 s ago (rewrite `granted_at`), B registers wait → `granted_to()` == B; A still listed with its seq; A pinged again.
  - `test_within_the_grace_the_woken_session_keeps_its_turn`: same at 9 s → `granted_to()` == A.
  - `test_a_free_mic_is_served_past_an_absent_callback_grant`: lone A callback granted 11 s ago; a `Conch` for session C (not queued) → `try_acquire()` is True.
  - `test_a_give_to_a_callback_session_holds_for_the_ttl`: `grant(A)` with A callback at 20 s and B wait → A; at 31 s → B.
  - `test_the_sixth_grant_event_sends_no_wake`: six `grant_next()` calls for the same queued A → five wakes.
  - `test_unpatched_grant_file_is_read_as_not_given_and_never_absent`: a grant dict with only `session_id`/`seq` → `granted_to()` == A forever (no `granted_at`).
  - `test_patch_is_idempotent_and_keeps_crlf` extended to `conch_queue.py` and `conch.py`.

- [ ] **Step 2: Run** with `VM812_SRC=<flat pristine dir>`: `.venv/bin/python -m pytest tests/test_conch_wakers.py -q`. Expected: the new tests FAIL.

- [ ] **Step 3: Implement** the five insertions in `patches/patch_conch_wakers.py` (one ANCHOR/ADD pair per target file; `apply()` dispatches on the file name; for `conch_queue.py` and `conch.py` several pairs, each counted exactly once). Update the manifests and the test registrations listed under Files.

- [ ] **Step 4: Run the full suite with release checks**

Run: `VM812_SRC=<flat> VOICEMODE_RELEASE_CHECK=1 .venv/bin/python -m pytest tests -q -p no:cacheprovider`
Expected: 0 failed.

- [ ] **Step 5: Commit** `git commit -m "feat(conch): ping a lone callback grantee; present callers pass an absent callback grant; gives are marked"`

### Task 3: Live round trip on WSL, docs, FELIX hand-off

**Files:**
- Modify: `CLAUDE.md` (Session Queue item 2: callback is real on Claude Code POSIX; grace 10 s; up to 5 wakes; a woken session may stay silent)
- Modify: `docs/felix-integration/README.md` (the v1.1.1 conch bullet: lone-grantee ping, present beats absent, exit 75)
- Modify: `docs/superpowers/specs/2026-11-11-conch-callback-design.md` (status: built; the live result with n)

- [ ] **Step 1: Apply to the live venv**: `python3 patches/apply.py .venv`; expected `[patches] Done.`
- [ ] **Step 2: Live test with Robert** (two interactive Claude Code sessions on WSL, both `/mcp` reconnected): session A speaks and holds; session B calls converse with `wait_for_conch=true, conch_mode="callback"` and keeps working; A releases. Expected each round: `~/.voicemode/logs/session-wake.log` shows `reason=conch ... ok=True` for B, and B speaks within about 10 s with its name. Repeat 3 times (n=3), once with B busy (expected: refused, A or a third session may talk, B keeps its seq per `voicemode conch status`).
- [ ] **Step 3: Update the docs** listed under Files with the measured results (n, times).
- [ ] **Step 4: Commit and push** `git commit -m "docs(conch): real callback validated live on WSL"`; then `gh auth switch --user rwunsch && git push`.
- [ ] **Step 5: Tell FELIX-voice** (SendMessage): new targets/unshare entries for the patch step, the exit-75 contract, and that FELIX still serves callback as wait until its waker is proven.
