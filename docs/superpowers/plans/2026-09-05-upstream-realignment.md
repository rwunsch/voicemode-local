# Upstream Realignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move voicemode-local from its 8.7.1 pin onto voice-mode 8.12.0, retire every patch upstream has since fixed, migrate our session queue onto upstream's `conch` queue, and upstream what genuinely remains.

**Architecture:** Three phases, strictly ordered. Phase A rebases onto 8.12.0 in a throwaway test bed and empirically classifies each of our 9 patch artifacts as obsolete / superseded / still-needed — nothing is upstreamed or rewritten until we know what survives. Phase B upstreams the survivors as PRs against `mbailey/voicemode:master`. Phase C rebases push-to-talk onto whatever upstream's control channel already gives us. Phase A is a hard gate on B and C: most of what we would have proposed in June is already merged.

**Tech Stack:** Python 3.12, `uv`/`uvx`, pytest, Docker Compose, `gh` CLI, voice-mode 8.12.0 (PyPI + `mbailey/voicemode` master).

**Spec:** This plan is its own spec — it is derived from the 2026-09-05 upstream audit recorded in "Findings" below rather than from a prior design doc. Prior related specs: `docs/superpowers/specs/2026-06-07-voice-session-queue-design.md`, `docs/superpowers/specs/2026-07-07-push-to-talk-design.md`.

## Global Constraints

- Target upstream version: **voice-mode 8.12.0** (released 2026-07-21; latest on PyPI and `master` HEAD as of 2026-09-05).
- Currently installed and patched: **voice-mode 8.7.1** (`.venv/lib/python3.12/site-packages/voice_mode-8.7.1.dist-info`).
- **Never mutate the working `.venv` until Task 8.** The user has live voice sessions against it; a broken venv means no voice at all. All Phase A work happens in `/tmp/.../scratchpad/vm812/`.
- Python interpreter for new venvs: `/usr/bin/python3.12` (system Python — brew Python venvs have broken here before; see memory `project_voicemode_venv_breakage`).
- Upstream PRs are pushed from the **`rwunsch`** GitHub account: run `gh auth switch --user rwunsch` before any push or PR creation.
- Upstream default branch is **`master`**, not `main`.
- No AI-attribution trailers in any commit or PR body (project rule).
- Every "obsolete" verdict must be backed by a **run test**, not a changelog line. The changelog is evidence; a passing/failing test is validation.

---

## Findings (2026-09-05 audit — validated)

Everything here was read from the installed package, the GitHub API, or upstream source today. It is the reason this plan exists and the reason it is not the plan we would have written in June.

**Upstream absorbed most of our work while we sat on 8.7.1.**

| Our artifact | Upstream status in 8.12.0 | Verdict |
|---|---|---|
| `patch_converse_cancel.py` | **VM-2015** (8.12.0): "`CancelledError` is now handled at the boundary the MCP SDK expects" | Almost certainly obsolete |
| `patch_listen_stall.py` | **8.11.0**: "a dead-stream stall backstop in the recording loop"; **VM-2015**: recording "cooperatively cancellable… 0.4s vs 17.5s" | Almost certainly obsolete |
| `patch_listen_overrun.py` | Related to the above; not explicitly matched in any changelog entry | Unknown — must test |
| `fcntl_shim.py`, `resource_shim.py` | **8.11.0**: `voice_mode/file_lock.py` — real cross-platform locking (`fcntl.flock` / `msvcrt.locking` at a high offset, SQLite's locking-byte trick) + psutil PID probes | Obsolete, and ours is the worse implementation |
| `voice_queue.py` + `patch_converse_queue.py` | **8.8.0 epic VM-1610**: `conch_queue.py` (696 lines), `conch_ops.py`, `conch_notify.py`, `cli_commands/conch.py`, `tools/conch.py`, `file_lock.py`, and 6 test files | **Superseded** — see below |
| `patch_shutdown_abort.py` | Partly VM-2015 (server no longer exits on cancel); orphan-stream reaping not obviously covered | Unknown — must test |
| `patch_audio_keepalive.py` | Open PR **#523** (moabian) covers adjacent ground; not merged | Likely still needed |
| `patch_simple_failover.py` | **VM-1556** (8.8.0) stopped the silent OpenAI *default*; the voice-swap-on-failover behaviour is a separate code path | Likely still needed |

**On the queue specifically — upstream's is not just equivalent, it is better.** Upstream 8.8.0 built the same design independently one week after our 8.7.1 pin: per-waiter files, atomic create/unlink, stale cleanup by PID liveness, a grant-hint file so waiters don't race. Ours keys order on `epoch-µs`; upstream allocates `seq` under an flock'd counter, which is immune to clock skew across machines and therefore works for remote agents — ours is not. Upstream also ships what we never built: `callback` mode (return now, get pinged when granted), notify-on-give into the agent's pane, fair promotion past idle callback-waiters, a `conch` MCP tool for remote agents, a `voicemode conch {status,give,bump,release,wait}` CLI, and 6 test files. Ours has 0 dedicated upstream-equivalent tests in that venv.

**Does upstream take PRs?** Sampling the last 60 merged PRs (merged 2026-05-11 → 2026-07-08): `ai-cora` 40, dependabot 16, external humans 3 (`blakechasteen` #496, `systematicguy` #494, `JimGaylard` #483). That is **5% external**. But when external PRs do land they land *fast* — 0–2 days from open to merge — and they are credited by name in the changelog. The bottleneck is attention, not hostility.

**Caveats that shape strategy.** There are 19 open PRs, 11 of them human, the oldest open since 2026-02-20 (#281), 2026-03-22 (#320), 2026-03-27 (#328) — i.e. 5–6 months. And `master` HEAD is **2026-07-21**: no public merge activity for ~6.5 weeks. The maintainer works via internal `VM-####` tickets merged as `Merge tag 'ready/...'` branches, not through GitHub PRs. So a PR is a lottery ticket with good odds *if* it gets looked at, and our fallback must be a patch set we can carry indefinitely.

**Open upstream demand we can serve.**
- **#312** "Support for push-to-talk and/or interruptible converse mode" — open since 2026-03-11.
- **#328** hold-mode PTT — open PR since 2026-03-27, unmerged.
- **#341** "Audio cuts off at ~19 seconds on WSLg / Docker Desktop" — open since 2026-04-16.
- **#342** "Mild crackling on WSLg after extended use" — open since 2026-04-16.
- **Piper: zero issues, zero PRs, zero mentions.** No demand signal at all.

---

# Phase A — Rebase onto 8.12.0

## Task 1: Build an isolated 8.12.0 test bed

**Files:**
- Create: `/tmp/claude-1000/-home-wunsch-git-voicemode-local/<session>/scratchpad/vm812/` (venv, disposable)
- Create: `docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md` (the facts file this phase writes to)

**Interfaces:**
- Produces: `$VM812` = path to a clean, unpatched voice-mode 8.12.0 `site-packages/voice_mode`, and `$VM812VENV` = its venv root. Every later task in Phase A consumes these.

- [ ] **Step 1: Create the venv on system Python and install 8.12.0**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
/usr/bin/python3.12 -m venv "$SB/vm812"
"$SB/vm812/bin/pip" install --quiet --upgrade pip
"$SB/vm812/bin/pip" install --quiet 'voice-mode==8.12.0'
"$SB/vm812/bin/python" -c "import voice_mode, pathlib; print(pathlib.Path(voice_mode.__file__).parent)"
```

- [ ] **Step 2: Verify it is 8.12.0 and unpatched**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
VM812="$SB/vm812/lib/python3.12/site-packages/voice_mode"
cat "$SB"/vm812/lib/python3.12/site-packages/voice_mode-*.dist-info/METADATA | grep '^Version:'
test ! -f "$VM812/voice_queue.py" && echo "OK: unpatched (no voice_queue.py)"
ls "$VM812" | grep -E 'conch|file_lock'
```

Expected: `Version: 8.12.0`; `OK: unpatched`; and `conch.py conch_notify.py conch_ops.py conch_queue.py file_lock.py` present.

- [ ] **Step 3: Snapshot the three files our patches target, for diffing**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
VM812="$SB/vm812/lib/python3.12/site-packages/voice_mode"
VM871=/home/wunsch/git/voicemode-local/.venv/lib/python3.12/site-packages/voice_mode
mkdir -p "$SB/diffs"
for f in tools/converse.py core.py server.py simple_failover.py; do
  diff -u "$VM871/$f" "$VM812/$f" > "$SB/diffs/$(echo $f | tr / _).diff" 2>&1
  echo "$f: $(wc -l < "$SB/diffs/$(echo $f | tr / _).diff") diff lines"
done
```

- [ ] **Step 4: Create the audit facts file**

```bash
cat > /home/wunsch/git/voicemode-local/docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md <<'EOF'
# Patch audit: voicemode-local patches vs voice-mode 8.12.0

Baseline: voice-mode 8.12.0, clean venv, /usr/bin/python3.12.
Each row is filled by Task 2. Verdict must cite a command + observed output,
never a changelog line alone.

| Patch | Anchors still present? | Behaviour test | Verdict | Evidence |
|---|---|---|---|---|
EOF
```

- [ ] **Step 5: Commit**

```bash
cd /home/wunsch/git/voicemode-local
git add docs/superpowers/plans/2026-09-05-upstream-realignment.md docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md
git commit -m "docs(upstream): realignment plan + patch audit facts file"
```

---

## Task 2: Classify every patch against 8.12.0

Each patcher is already its own drift detector: it exits 1 naming the anchor when upstream has moved. That gives us a free first-pass signal, but an anchor surviving does **not** mean the patch is still needed — the bug may be fixed elsewhere. Both checks are required.

**Files:**
- Modify: `docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md`
- Read: `patches/patch_*.py`

**Interfaces:**
- Consumes: `$VM812` from Task 1.
- Produces: a filled audit table with one verdict per patch from the set `{OBSOLETE, SUPERSEDED, STILL-NEEDED, NEEDS-REWRITE}`. Task 3 consumes `OBSOLETE`/`SUPERSEDED`; Task 5 consumes `STILL-NEEDED`/`NEEDS-REWRITE`.

- [ ] **Step 1: Run every patcher against a scratch copy and record anchor survival**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
VM812="$SB/vm812/lib/python3.12/site-packages/voice_mode"
P=/home/wunsch/git/voicemode-local/patches
rm -rf "$SB/probe" && cp -r "$VM812" "$SB/probe"
for pair in "patch_converse_queue:tools/converse.py" \
            "patch_converse_cancel:tools/converse.py" \
            "patch_listen_overrun:tools/converse.py" \
            "patch_listen_stall:tools/converse.py" \
            "patch_shutdown_abort:server.py" \
            "patch_audio_keepalive:core.py" \
            "patch_simple_failover:simple_failover.py"; do
  name=${pair%%:*}; target=${pair##*:}
  if "$SB/vm812/bin/python" "$P/$name.py" "$SB/probe/$target" >"$SB/probe.$name.log" 2>&1; then
    echo "ANCHORS-OK   $name"
  else
    echo "ANCHORS-DRIFT $name -> $(tail -1 "$SB/probe.$name.log")"
  fi
done
```

Expected: a mix. Record every line verbatim into the audit table's "Anchors still present?" column.

- [ ] **Step 2: Write the behaviour test for the cancel fix (VM-2015)**

This is the decisive test for `patch_converse_cancel.py`. Upstream claims a `CancelledError` now unwinds one request instead of killing the server. Create `tests/upstream_audit/test_vm2015_cancel.py` in the repo:

```python
"""Does 8.12.0 still need patch_converse_cancel.py?

Upstream VM-2015 claims CancelledError is handled at the MCP SDK boundary.
If so, converse() must RE-RAISE CancelledError rather than returning a result
(returning one is what double-responds and kills the server).
"""
import ast
import inspect
from pathlib import Path

import pytest


def _converse_source(vm_dir: Path) -> str:
    return (vm_dir / "tools" / "converse.py").read_text()


def test_cancellederror_is_reraised_not_swallowed(vm812_dir):
    """A bare `except asyncio.CancelledError:` that returns is the 8.7.1 bug."""
    tree = ast.parse(_converse_source(vm812_dir))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        name = ast.dump(node.type or ast.Constant(None))
        if "CancelledError" not in name:
            continue
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        raises = [n for n in ast.walk(node) if isinstance(n, ast.Raise)]
        if returns and not raises:
            offenders.append(node.lineno)
    assert not offenders, (
        f"CancelledError handler(s) at line(s) {offenders} return instead of "
        "re-raising — patch_converse_cancel.py is STILL NEEDED"
    )
```

- [ ] **Step 3: Add the fixture that points tests at the 8.12.0 test bed**

Create `tests/upstream_audit/conftest.py`:

```python
import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def vm812_dir() -> Path:
    """The clean 8.12.0 voice_mode package built by Task 1."""
    raw = os.environ.get("VM812_DIR")
    if not raw:
        pytest.skip("VM812_DIR not set — run Task 1 first")
    p = Path(raw)
    if not (p / "tools" / "converse.py").exists():
        pytest.fail(f"VM812_DIR={p} does not look like a voice_mode package")
    return p
```

- [ ] **Step 4: Run it and record the verdict**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
cd /home/wunsch/git/voicemode-local
VM812_DIR="$SB/vm812/lib/python3.12/site-packages/voice_mode" \
  .venv/bin/python -m pytest tests/upstream_audit/test_vm2015_cancel.py -v
```

Expected: PASS means upstream fixed it → verdict `OBSOLETE` for `patch_converse_cancel.py`. FAIL means keep it. Record the command and its output in the audit file.

- [ ] **Step 5: Write the same shape of test for the recording-stall backstop**

Create `tests/upstream_audit/test_811_stall_backstop.py`:

```python
"""Does 8.12.0 still need patch_listen_stall.py / patch_listen_overrun.py?

8.11.0 claims "a dead-stream stall backstop in the recording loop".
The 8.7.1 bug: recording_duration only advances when a chunk is dequeued, so a
starved capture callback freezes it and `while recording_duration < max_duration`
never exits. A real backstop must consult a wall-clock source inside that loop.
"""
import re
from pathlib import Path


def _loop_body(vm_dir: Path) -> str:
    src = (vm_dir / "tools" / "converse.py").read_text()
    m = re.search(r"def record_audio_with_silence_detection.*?\n(?=\ndef |\nclass )", src, re.S)
    assert m, "record_audio_with_silence_detection not found — upstream restructured"
    return m.group(0)


def test_recording_loop_has_a_wallclock_backstop(vm812_dir):
    body = _loop_body(vm812_dir)
    has_wallclock = bool(re.search(r"time\.(monotonic|time)\(\)", body))
    assert has_wallclock, (
        "no wall-clock reference inside record_audio_with_silence_detection — "
        "patch_listen_stall.py is STILL NEEDED"
    )


def test_speech_is_not_truncated_at_max_duration(vm812_dir):
    """The overrun fix: once speech started, max_duration must not hard-cut."""
    body = _loop_body(vm812_dir)
    naive_cap = re.search(r"while\s+recording_duration\s*<\s*max_duration\s*(and|:)", body)
    assert not naive_cap, (
        "loop still bounded by a bare `recording_duration < max_duration` — "
        "patch_listen_overrun.py is STILL NEEDED"
    )
```

- [ ] **Step 6: Run it and record**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
cd /home/wunsch/git/voicemode-local
VM812_DIR="$SB/vm812/lib/python3.12/site-packages/voice_mode" \
  .venv/bin/python -m pytest tests/upstream_audit/ -v
```

Record both results in the audit file with the exact command.

- [ ] **Step 7: Classify the two Windows shims by inspection**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
VM812="$SB/vm812/lib/python3.12/site-packages/voice_mode"
grep -rn "^import fcntl\|^import resource" "$VM812" --include=*.py | grep -v file_lock.py
```

Expected: no hits outside `file_lock.py` (which guards its import by platform). If so, both shims are `OBSOLETE` — upstream's `file_lock.py` does it properly. Record the command and output.

- [ ] **Step 8: Commit the audit**

```bash
cd /home/wunsch/git/voicemode-local
git add tests/upstream_audit/ docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md
git commit -m "test(upstream): audit voicemode-local patches against voice-mode 8.12.0"
```

---

## Task 3: Retire the obsolete patches

**Files:**
- Delete: every patch file the Task 2 audit marked `OBSOLETE` (expected: `patch_converse_cancel.py`, `patch_listen_stall.py`, `fcntl_shim.py`, `resource_shim.py` — confirm against the audit, do not assume)
- Modify: `patches/apply.sh` — remove the corresponding blocks
- Modify: `CLAUDE.md`, `README.md` — drop the claims these patches backed

**Interfaces:**
- Consumes: the audit table's verdicts from Task 2.
- Produces: an `apply.sh` that runs clean against 8.12.0 for the reduced patch set.

- [ ] **Step 1: Delete each OBSOLETE patch and its apply.sh block**

For each patch marked `OBSOLETE`, remove the file and the guarded block in `patches/apply.sh` that invokes it. The blocks are self-contained `if [ -f "$SCRIPT_DIR/<name>.py" ]; then … fi` stanzas plus their leading comment.

- [ ] **Step 2: Verify apply.sh still runs clean on a fresh 8.12.0 copy**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
rm -rf "$SB/vm812-apply" && /usr/bin/python3.12 -m venv "$SB/vm812-apply"
"$SB/vm812-apply/bin/pip" install --quiet 'voice-mode==8.12.0'
/home/wunsch/git/voicemode-local/patches/apply.sh "$SB/vm812-apply"; echo "exit=$?"
```

Expected: `exit=0`. A non-zero exit names the anchor that drifted — that patch belongs to Task 5, not here.

- [ ] **Step 3: Fix the README comparison table**

The "What VoiceMode Local Adds" table in `README.md` claims upstream STT is "OpenAI Whisper API (cloud)" and TTS is "OpenAI only". Both are false for 8.12.0: `config.py` defaults are `TTS_BASE_URLS=http://127.0.0.1:8880/v1,https://api.openai.com/v1` and `STT_BASE_URLS=http://127.0.0.1:2022/v1,…`, and upstream ships `tools/whisper/install.py` (builds whisper.cpp) and `tools/kokoro/install.py`. Delete the STT, TTS, Cost, Privacy and "Voice engines" rows and replace the table with an honest one: Piper/multilingual, Docker-vs-native-build deployment, WSLg-specific fixes, `voicemode-switch` compute modes.

- [ ] **Step 4: Commit**

```bash
cd /home/wunsch/git/voicemode-local
git add -A patches/ README.md CLAUDE.md
git commit -m "chore(patches): retire patches fixed upstream in 8.11/8.12; correct README claims"
```

---

## Task 4: Migrate the session queue onto upstream `conch`

This is the largest deletion in the plan: 939 lines of `voice_queue.py` plus an 11KB patcher, replaced by configuration and a thin CLI shim. Do not skip the behaviour parity check — the queue is the feature the user actually relies on daily across parallel sessions.

**Files:**
- Delete: `patches/voice_queue.py`, `patches/patch_converse_queue.py`
- Modify: `patches/apply.sh` (drop both blocks)
- Modify: `voicemode-switch` — repoint the `queue` and `floor reset` subcommands at `voicemode conch`
- Modify: `CLAUDE.md` — rewrite the "Session Queue" section against upstream semantics
- Create: `tests/test_conch_parity.py`

**Interfaces:**
- Consumes: 8.12.0's `voice_mode.conch_queue.ConchQueue`, the `voicemode conch {status,give,bump,release,wait}` CLI, and `converse`'s `hold_conch` parameter.
- Produces: `voicemode-switch queue` → `voicemode conch status`; `voicemode-switch floor reset` → `voicemode conch release`. Phase C's PTT work consumes these names.

- [ ] **Step 1: Map our semantics onto upstream's, in writing**

Add a mapping table to the audit file. Known correspondences from upstream's own docs and source:

| voicemode-local | upstream 8.12.0 |
|---|---|
| ticket file `~/.voicemode/queue/<epoch-µs>-<pid>.json` | `~/.voicemode/conch.queue.d/<seq>-<session>.json` |
| `~/.voicemode/floor.json` | `~/.voicemode/conch` (holder lock) + `~/.voicemode/conch.grant` |
| QUEUED status + re-call with `ticket` | `wait` mode (block) or `callback` mode (return now, get pinged) |
| `end_burst=true` (hand off the mic) | release-on-turn-end; `hold_conch=true` is the inverse (keep it) |
| wedged-floor detection via heartbeat | `VOICEMODE_CONCH_GRANT_TTL` (default 30s) + idle hold lapse (default 10s) |
| `voicemode-switch queue` | `voicemode conch status` |
| `voicemode-switch floor reset` | `voicemode conch release` |
| *(none — we never built it)* | `voicemode conch give <session>`, notify-on-give, `conch` MCP tool |

Flag any row where upstream has **no** equivalent — that, and only that, is a candidate for a follow-on upstream PR.

- [ ] **Step 2: Write the parity test before deleting anything**

Create `tests/test_conch_parity.py`:

```python
"""Upstream conch must cover the queue behaviours we depend on daily."""
from pathlib import Path

import pytest


def test_conch_queue_module_exists(vm812_dir: Path):
    assert (vm812_dir / "conch_queue.py").exists()


def test_conch_cli_exposes_status_give_release(vm812_dir: Path):
    src = (vm812_dir / "cli_commands" / "conch.py").read_text()
    for sub in ("status", "give", "release", "wait", "bump"):
        assert f'"{sub}"' in src or f"'{sub}'" in src or f"def {sub}" in src, (
            f"conch CLI is missing the `{sub}` subcommand"
        )


def test_ordering_is_seq_based_not_clock_based(vm812_dir: Path):
    """Ours keyed order on epoch-µs; upstream must use a locked counter."""
    src = (vm812_dir / "conch_queue.py").read_text()
    assert "conch.queue.seq" in src, "no monotonic seq counter — ordering may race"
```

- [ ] **Step 3: Run it against the 8.12.0 test bed**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
cd /home/wunsch/git/voicemode-local
VM812_DIR="$SB/vm812/lib/python3.12/site-packages/voice_mode" \
  .venv/bin/python -m pytest tests/test_conch_parity.py -v
```

Expected: all PASS. Any FAIL is a genuine gap — record it as a candidate upstream PR in Phase B and keep the corresponding slice of `voice_queue.py` rather than deleting wholesale.

- [ ] **Step 4: Delete the queue patch set**

```bash
cd /home/wunsch/git/voicemode-local
git rm patches/voice_queue.py patches/patch_converse_queue.py
```

Then remove both `if [ -f … ]` blocks from `patches/apply.sh`, including the long explanatory comment above the `voice_queue.py` copy.

- [ ] **Step 5: Repoint voicemode-switch**

In `voicemode-switch`, change the `queue` subcommand to shell out to `voicemode conch status` and `floor reset` to `voicemode conch release`, preserving our output formatting so the user's muscle memory survives.

- [ ] **Step 6: Rewrite the CLAUDE.md Session Queue section**

Replace the two "non-negotiable rules" with upstream's contract: `wait` vs `callback` mode, `hold_conch` to keep the floor across turns, and the fact that the floor now lapses on a 10s idle timeout rather than our 90s auto-release. This is a user-facing behaviour change and must be called out.

- [ ] **Step 7: Commit**

```bash
cd /home/wunsch/git/voicemode-local
git add -A
git commit -m "refactor(queue): migrate from voice_queue to upstream conch queue (VM-1610)

Deletes 939 lines of voice_queue.py and its patcher. Upstream 8.8.0 built the
same design independently — per-waiter files, atomic register/deregister,
grant hint, stale cleanup — and allocates order under an flock'd counter
rather than epoch-us, so it is correct across machines where ours was not.
It also ships callback mode, notify-on-give, fair promotion, an MCP tool and
a CLI we never built."
```

---

## Task 5: Re-anchor the surviving patches

**Files:**
- Modify: each patch file the audit marked `NEEDS-REWRITE` (expected: `patch_audio_keepalive.py`, `patch_simple_failover.py`, and possibly `patch_shutdown_abort.py` / `patch_listen_overrun.py`)

**Interfaces:**
- Consumes: the drift messages captured in Task 2 Step 1 and the diffs in `$SB/diffs/`.
- Produces: an `apply.sh` that exits 0 against a clean 8.12.0 venv.

- [ ] **Step 1: For each drifted patch, read the new upstream code around the old anchor**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
less "$SB/diffs/core.py.diff"          # for patch_audio_keepalive
less "$SB/diffs/simple_failover.py.diff"
```

- [ ] **Step 2: Update the anchor strings, keeping the fail-loud contract**

Each patcher must still exit 1 naming the anchor when it cannot find it, and must still be idempotent (re-running on a patched file exits 0). Do not relax either property to make a patch apply — a silently mis-applied patch is worse than a failed one.

- [ ] **Step 3: Verify each patch applies and is idempotent**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
rm -rf "$SB/vm812-reanchor" && /usr/bin/python3.12 -m venv "$SB/vm812-reanchor"
"$SB/vm812-reanchor/bin/pip" install --quiet 'voice-mode==8.12.0'
/home/wunsch/git/voicemode-local/patches/apply.sh "$SB/vm812-reanchor"; echo "first=$?"
/home/wunsch/git/voicemode-local/patches/apply.sh "$SB/vm812-reanchor"; echo "second=$?"
```

Expected: `first=0` and `second=0`.

- [ ] **Step 4: Update the "Anchors verified against" line in each patch docstring**

Every surviving patcher's docstring ends with `Anchors verified against voice-mode 8.7.1.` — change to `8.12.0`.

- [ ] **Step 5: Commit**

```bash
cd /home/wunsch/git/voicemode-local
git add patches/
git commit -m "fix(patches): re-anchor surviving patches against voice-mode 8.12.0"
```

---

## Task 6: Run the full test suite against the rebased stack

**Files:**
- Modify: `tests/` — any test that asserts 8.7.1-specific behaviour

- [ ] **Step 1: Run the repo suite**

```bash
cd /home/wunsch/git/voicemode-local
.venv/bin/python -m pytest tests/ -v 2>&1 | tail -40
```

- [ ] **Step 2: Fix or delete tests that assert retired behaviour**

Tests covering `voice_queue.py` are now testing deleted code — delete them. Tests covering surviving patches must be updated to the new anchors.

- [ ] **Step 3: Re-run to green**

```bash
cd /home/wunsch/git/voicemode-local
.venv/bin/python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all pass, with the count recorded in the audit file.

- [ ] **Step 4: Commit**

```bash
cd /home/wunsch/git/voicemode-local
git add tests/
git commit -m "test: align suite with the 8.12.0 rebase"
```

---

## Task 7: Verify the live voice path end to end, still in the test bed

**Interfaces:**
- Consumes: the Docker services (whisper :2022 proxy, kokoro :8880, piper :8881) already running against the live install — these are version-independent and shared.

- [ ] **Step 1: Confirm the local backends answer**

```bash
curl -s -o /dev/null -w 'whisper-proxy %{http_code}\n' http://127.0.0.1:2022/v1/models
curl -s -o /dev/null -w 'kokoro      %{http_code}\n' http://127.0.0.1:8880/v1/audio/voices
curl -s -o /dev/null -w 'piper-proxy %{http_code}\n' http://127.0.0.1:8881/v1/audio/voices
```

- [ ] **Step 2: Synthesize through the rebased venv**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
"$SB/vm812-reanchor/bin/voicemode" converse --skip-stt --voice af_sky "Rebase test on eight twelve."
```

Expected: audible speech in `af_sky`, no OpenAI substitution, no stutter.

- [ ] **Step 3: Verify Piper still routes**

```bash
SB=/tmp/claude-1000/-home-wunsch-git-voicemode-local/62357c07-dcc1-45c7-b0a9-78869b1b4a95/scratchpad
"$SB/vm812-reanchor/bin/voicemode" converse --skip-stt --voice p_de_thorsten "Guten Tag, dies ist ein Test."
```

Expected: German, in the Piper voice — not an English OpenAI fallback. A fallback here means `patch_simple_failover.py` did not re-anchor correctly; return to Task 5.

- [ ] **Step 4: Record results in the audit file and commit**

```bash
cd /home/wunsch/git/voicemode-local
git add docs/superpowers/plans/artifacts/2026-09-05-patch-audit.md
git commit -m "docs(upstream): record 8.12.0 end-to-end verification results"
```

---

## Task 8: Cut the live install over to 8.12.0

**This is the only task that touches the working `.venv`.** Do it when the user is not mid-session.

- [ ] **Step 1: Back up the current venv**

```bash
cd /home/wunsch/git/voicemode-local
mv .venv .venv.8.7.1-backup
```

- [ ] **Step 2: Rebuild on system Python at 8.12.0**

```bash
cd /home/wunsch/git/voicemode-local
/usr/bin/python3.12 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet 'voice-mode==8.12.0'
./patches/apply.sh; echo "apply=$?"
```

Expected: `apply=0`.

- [ ] **Step 3: Restart Claude Code and confirm voice works**

Patches only take effect on a fresh MCP server. Have the user restart, then run one real `converse` exchange.

- [ ] **Step 4: Remove the backup once a full session has run clean**

```bash
rm -rf /home/wunsch/git/voicemode-local/.venv.8.7.1-backup
```

- [ ] **Step 5: Commit and push**

```bash
cd /home/wunsch/git/voicemode-local
git add -A
git commit -m "chore: pin voice-mode 8.12.0"
gh auth switch --user rwunsch
git push origin feature/piper-integration
```

---

# Phase B — Upstream what survives

**Gate: do not start Phase B until Task 2's audit is filled in.** Every candidate below is contingent on it surviving the audit.

Strategy note: upstream merges 5% of external PRs but merges them in 0–2 days when it does, and credits contributors by name in the changelog. Three of our four candidates attach to an **already-open upstream issue**, which is the strongest available signal that a PR will be looked at. Piper does not, so it gets an issue first, not a PR.

## Task 9: WSLg audio fixes → issues #341 and #342

The best-targeted contribution we have. Upstream has two open issues describing symptoms we diagnosed and fixed: #341 "Audio cuts off at ~19 seconds on WSLg / Docker Desktop" and #342 "Mild crackling on WSLg after extended use". Our memory files record six distinct root causes for WSL stutter, several with validated fixes.

- [ ] **Step 1: Comment on #341 and #342 with the diagnosis before opening any PR**

Lead with the mechanism, not the patch — a maintainer at 5% merge rate reads a diagnosis faster than a diff. Draw from the validated findings: orphaned WSLg RDPSink playback streams from duplicate voice-mode processes, streaming PCM playback having no jitter buffer, and CPU oversubscription starving the unpinnable WSLg audio transport. Note which are already fixed by 8.11/8.12 and which are not.

- [ ] **Step 2: Open the PR only for what survived Task 2, against `master`**

```bash
gh auth switch --user rwunsch
gh repo fork mbailey/voicemode --clone=false
```

Scope the PR to one fix. Reference the issue number in the title. Do not bundle Piper, Docker or PTT into it.

## Task 10: Push-to-talk → issue #312

We have a complete PTT implementation on `feature/push-to-talk`: 17 commits, 1,984 lines, 9 test files, covering a press/hold/release state machine, a TCP relay bus, an X11 Linux listener, a Windows listener, a WSL2 companion, barge-in (interrupt TTS on press), and terminal focus scoping.

- [ ] **Step 1: Read PR #328 before writing anything**

```bash
gh pr view 328 -R mbailey/voicemode --json title,body,files,comments
gh pr diff 328 -R mbailey/voicemode | head -200
```

#328 is hold-mode PTT, open since 2026-03-27. If it substantially overlaps ours, the right move is to **review and support #328** rather than open a competing PR — a second unmerged PTT PR helps nobody.

- [ ] **Step 2: Check what 8.11's control channel already gives us**

8.11.0 shipped `pause`/`resume`/`stop`/`skip-forward`/`skip-back` over a local socket, and `skip-forward` already means "pressed while you speak, ends the recording immediately and transcribes". That is a large part of PTT's plumbing. Read `docs/reference/control-channel.md` upstream and determine whether our PTT should be re-expressed as a control-channel client rather than a converse patch — that would make it far more mergeable.

- [ ] **Step 3: Comment on #312 with the design, and ask before building**

## Task 11: Piper — open an issue, not a PR

Piper has **zero** upstream issues, PRs or mentions. Upstream's multilingual answer is Kokoro plus Cartesia (8.8.0) and mlx-audio. There is no demand signal, and a large unsolicited PR adding a whole TTS engine to a repo with 11 stale open PRs will not land.

- [ ] **Step 1: Open a feature-request issue describing the gap, not the code**

Frame it as: Kokoro's non-English voices vs Piper's native ones for German/Dutch/Polish/Russian/Korean, and note that Cartesia solved the "add a provider" shape already, so the integration surface exists.

- [ ] **Step 2: Only build the PR if a maintainer responds positively**

Otherwise Piper stays ours. That is a perfectly good outcome — it is the one feature that genuinely differentiates voicemode-local.

## Task 12: Docker — do not upstream

Recorded here as an explicit decision so it is not revisited. Upstream deliberately builds whisper.cpp from source and manages services via systemd/launchd; `tools/whisper/install.py` and `tools/kokoro/install.py` are substantial and load-bearing. Our Docker stack is not a missing feature upstream wants, it is a *different deployment philosophy* — and the two actively conflict (the `:8880` collision between a native systemd Kokoro and a Docker container burned 16,174 failed service starts over 14 days).

Docker stays in voicemode-local. The one thing worth upstreaming from it is a **docs note** warning that a Docker Kokoro on `:8880` will fight `voicemode service start kokoro` — that is cheap, useful, and merges easily.

---

# Phase C — Push-to-talk for the user, now

**Gate: Phase A Task 8 complete.** PTT is on a branch built against 8.7.1 and patches `converse.py`, which 8.12.0 restructured (VM-1961 split `converse()` into a thin `@mcp.tool()` wrapper plus `_converse()`).

## Task 13: Rebase feature/push-to-talk onto the 8.12.0 patch set

- [ ] **Step 1: Rebase the branch**

```bash
cd /home/wunsch/git/voicemode-local
git checkout feature/push-to-talk
git rebase feature/piper-integration
```

- [ ] **Step 2: Re-anchor the two PTT patchers against 8.12.0**

`patches/patch_converse_ptt.py` and `patches/patch_core_ptt.py` target `converse.py` and `core.py`. VM-1961 split `converse()` — expect both to drift. Apply the Task 5 method: read the diff, update anchors, keep fail-loud and idempotent.

- [ ] **Step 3: Evaluate re-expressing PTT on the control channel**

Before re-anchoring by brute force, check whether `skip-forward` plus the control socket can replace the `converse.py` patch entirely. A PTT that is a control-channel client instead of a monkey-patch survives every future upgrade and is the version worth sending upstream.

- [ ] **Step 4: Run the PTT suite**

```bash
cd /home/wunsch/git/voicemode-local
.venv/bin/python -m pytest tests/test_ptt_*.py tests/test_converse_ptt_patch.py tests/test_core_ptt_patch.py -v
```

- [ ] **Step 5: Live-test the hotkey, then merge**

`VOICEMODE_PTT_ENABLED` is not hot-reloadable — set it in `~/.voicemode/voicemode.env` and restart Claude Code. Confirm: think silently, press and hold, speak, release, hear the reply. Then merge to `feature/piper-integration`.

---

## Self-Review

**Spec coverage.** Every question this plan was written to answer maps to a task: does upstream take PRs (Findings); are many open (Findings); review and upstream our work (Tasks 9–12); Piper and Docker (Tasks 11, 12); push-to-talk (Tasks 10, 13); patch the most recent voicemode (Tasks 1–8); is arbitration better than the queue (Findings + Task 4).

**Known contingency.** Tasks 3 and 5 are deliberately parameterised on Task 2's audit rather than naming a fixed file list, because the verdicts are not yet measured. This is the one place the plan defers detail, and it defers it to a measurement, not to a later opinion. Task 2 produces that measurement before either task runs.

**Ordering risk.** Task 8 is the only irreversible step and it is last in Phase A, behind a backup and an end-to-end verification.
