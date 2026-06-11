# Voice Session Names Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each concurrent Claude Code voice session announce a distinguishing label (its "session name") on handoff instead of all sessions in the same repo announcing the folder name.

**Architecture:** `voice_queue.session_project()` resolves a name via a 3-step fallback — `VOICEMODE_SESSION_NAME` env → a sidecar file `~/.voicemode/session_names/<CLAUDE_CODE_SESSION_ID>.txt` → the cwd folder name. The MCP server inherits its launching session's `CLAUDE_CODE_SESSION_ID` at spawn, so a label Claude writes to that file (keyed by the same ID) reaches the server without any env mutation. A CLAUDE.md instruction tells Claude to write an auto-derived label at voice-mode start. The spoken intro is unchanged — it already fires only after a real wait (multi-session contention).

**Tech Stack:** Python 3.10+, pytest (`tmp_path`/`monkeypatch` fixtures), bash. No new dependencies.

---

## Background for the implementer

- `patches/voice_queue.py` is the cross-session voice queue. It is copied into the
  installed `voice_mode/` package by `patches/apply.sh`; tests import it directly from
  `patches/` (see `tests/test_voice_queue.py` header).
- The function we change today is:
  ```python
  def session_project() -> str:
      return os.getenv("VOICEMODE_SESSION_NAME") or Path(os.getcwd()).name
  ```
  It is the *only* place the session label is computed. It is called once, from
  `QueueSession.__init__`:
  ```python
  self.project = project or session_project()
  ```
  `QueueSession` already stores `self.base` (the `~/.voicemode` dir, overridable for tests).
- The spoken intro lives in `QueueSession.intro` and is prepended in
  `patches/patch_converse_queue.py` ONLY when `_q.waited` is true. **Do not touch the
  intro or the patch** — "announce only when multiple sessions" is already satisfied.
- Tests run from the repo root with `pytest`. Existing queue tests use `tmp_path` as the
  `base` dir and `monkeypatch` for env/cwd.

Design doc: `docs/superpowers/specs/2026-06-11-voice-session-name-design.md`.

## File Structure

- **Modify** `patches/voice_queue.py`
  - Add two module constants near the other config constants.
  - Add a private GC helper `_gc_session_names(base)`.
  - Rewrite `session_project(base=None)` with the 3-step fallback + GC call.
  - Update the one caller `QueueSession.__init__` to pass `self.base`.
- **Create** `tests/test_session_name.py` — unit tests for the fallback chain + GC.
- **Modify** `CLAUDE.md` — extend "Voice Selection on Startup" with the label-write
  instruction.

---

## Task 1: `session_project()` fallback chain + GC

**Files:**
- Modify: `patches/voice_queue.py` (constants near line 51; `session_project` at lines 425-426; `QueueSession.__init__` near line 450)
- Test: `tests/test_session_name.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_name.py`:

```python
"""Tests for voice_queue.session_project() session-name resolution."""
import os
import sys
import time
from pathlib import Path

import pytest

PATCHES_DIR = Path(__file__).parent.parent / "patches"
sys.path.insert(0, str(PATCHES_DIR))

import voice_queue  # noqa: E402

SID = "test-session-id-1234"


def _write_label(base: Path, sid: str, text: str) -> Path:
    d = base / voice_queue.SESSION_NAMES_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.txt"
    f.write_text(text)
    return f


def test_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEMODE_SESSION_NAME", "  from-env  ")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    _write_label(tmp_path, SID, "from-file")
    # env wins and is trimmed; file ignored
    assert voice_queue.session_project(tmp_path) == "from-env"


def test_file_used_when_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    _write_label(tmp_path, SID, "  queue-naming\n")
    assert voice_queue.session_project(tmp_path) == "queue-naming"


def test_folder_name_when_no_env_no_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.chdir(tmp_path)
    assert voice_queue.session_project(tmp_path) == tmp_path.name


def test_empty_file_falls_back_to_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    monkeypatch.chdir(tmp_path)
    _write_label(tmp_path, SID, "   \n")
    assert voice_queue.session_project(tmp_path) == tmp_path.name


def test_no_session_id_uses_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    _write_label(tmp_path, SID, "ignored")  # present but no SID to key on
    assert voice_queue.session_project(tmp_path) == tmp_path.name


def test_gc_removes_old_label_keeps_fresh(tmp_path, monkeypatch):
    monkeypatch.delenv("VOICEMODE_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
    old = _write_label(tmp_path, "old-sid", "old")
    fresh = _write_label(tmp_path, SID, "fresh")
    # Backdate the old file well past the max age.
    past = time.time() - voice_queue.SESSION_NAME_MAX_AGE - 100
    os.utime(old, (past, past))
    # Resolving triggers opportunistic GC.
    assert voice_queue.session_project(tmp_path) == "fresh"
    assert not old.exists()
    assert fresh.exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/wunsch/git/voicemode-local && python -m pytest tests/test_session_name.py -v`
Expected: FAIL — `AttributeError: module 'voice_queue' has no attribute 'SESSION_NAMES_DIR'` (and `session_project()` takes no `base` arg).

- [ ] **Step 3: Add constants**

In `patches/voice_queue.py`, after the `LISTEN_CAP` / `DEFAULT_BASE` block (around line 52), add:

```python
# Per-session human label: Claude writes a short name to
# <base>/session_names/<CLAUDE_CODE_SESSION_ID>.txt at voice-mode start so
# concurrent sessions in the same repo are distinguishable on handoff. The MCP
# server inherits its launching session's CLAUDE_CODE_SESSION_ID, so it reads
# the same file. session_project() resolves env → this file → folder name.
SESSION_NAMES_DIR = "session_names"
SESSION_NAME_MAX_AGE = float(
    os.getenv("VOICEMODE_SESSION_NAME_MAX_AGE", str(7 * 24 * 3600)))  # seconds
```

- [ ] **Step 4: Add the GC helper and rewrite `session_project`**

Replace the existing definition (lines 425-426):

```python
def session_project() -> str:
    return os.getenv("VOICEMODE_SESSION_NAME") or Path(os.getcwd()).name
```

with:

```python
def _gc_session_names(base: Path) -> None:
    """Best-effort: drop label files older than SESSION_NAME_MAX_AGE so the
    directory doesn't accumulate one file per historical session. Never raises."""
    try:
        d = base / SESSION_NAMES_DIR
        if not d.is_dir():
            return
        cutoff = time.time() - SESSION_NAME_MAX_AGE
        for f in d.glob("*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


def session_project(base: Optional[Path] = None) -> str:
    """Resolve this session's voice label.

    1. VOICEMODE_SESSION_NAME env (explicit / launch-time override) — wins.
    2. <base>/session_names/<CLAUDE_CODE_SESSION_ID>.txt — the label Claude
       wrote at voice-mode start; the MCP server inherited the same session id
       at spawn, so it keys on its own CLAUDE_CODE_SESSION_ID.
    3. cwd folder name (previous behaviour).
    File read is best-effort and never raises into the voice path.
    """
    name = os.getenv("VOICEMODE_SESSION_NAME")
    if name and name.strip():
        return name.strip()
    sid = os.getenv("CLAUDE_CODE_SESSION_ID")
    if sid:
        base = Path(base) if base else DEFAULT_BASE
        _gc_session_names(base)
        try:
            label = (base / SESSION_NAMES_DIR / f"{sid}.txt").read_text().strip()
            if label:
                return label
        except OSError:
            pass
    return Path(os.getcwd()).name
```

- [ ] **Step 5: Pass the session's base from the caller**

In `QueueSession.__init__`, change:

```python
        self.project = project or session_project()
```

to:

```python
        self.project = project or session_project(self.base)
```

(This line is *after* `self.base` is assigned — verify ordering; `self.base` is set on the first line of `__init__`.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /home/wunsch/git/voicemode-local && python -m pytest tests/test_session_name.py -v`
Expected: PASS — all 6 tests green.

- [ ] **Step 7: Run the full queue suite for regressions**

Run: `cd /home/wunsch/git/voicemode-local && python -m pytest tests/test_voice_queue.py -v`
Expected: PASS — no regressions (existing `session_project()` zero-arg behaviour preserved via the default `base=None`).

- [ ] **Step 8: Commit**

```bash
cd /home/wunsch/git/voicemode-local
git add patches/voice_queue.py tests/test_session_name.py
git commit -m "feat(queue): resolve session name via env → sidecar file → folder

session_project() now reads a per-session label from
session_names/<CLAUDE_CODE_SESSION_ID>.txt when VOICEMODE_SESSION_NAME is
unset, so concurrent sessions in one repo are distinguishable on handoff.
Old label files are GC'd past SESSION_NAME_MAX_AGE."
```

---

## Task 2: CLAUDE.md voice-startup instruction

**Files:**
- Modify: `CLAUDE.md` ("Voice Selection on Startup" section)

- [ ] **Step 1: Add the label-write instruction**

In `CLAUDE.md`, find the "## Voice Selection on Startup" section. Immediately after its
intro blockquote (the "Starting voice mode…" line) and before the "Random voice" bullet,
add:

```markdown
**Also set a session label** so concurrent voice sessions are distinguishable when they
hand off the mic. At voice-mode start, derive a short 1–3 word label for what this session
is about (e.g. `queue-naming`, `windows-port`); fall back to the repo name if nothing
specific stands out. Write it once:

​```bash
mkdir -p ~/.voicemode/session_names && \
  printf '%s' '<label>' > ~/.voicemode/session_names/"$CLAUDE_CODE_SESSION_ID".txt
​```

This label is what a session announces on handoff ("This is <label>, <voice> —") and is
spoken only when another session is waiting. If the user later says "call this session X",
rewrite the file with the new label — it takes effect on the next exchange, no restart.
```

(Remove the zero-width characters before each ``` fence shown above — they are only to
keep this code block from closing the plan's fence. The real file uses plain ``` fences.)

- [ ] **Step 2: Verify the section reads correctly**

Run: `cd /home/wunsch/git/voicemode-local && sed -n '/Voice Selection on Startup/,/Switching Voices/p' CLAUDE.md`
Expected: the new instruction and bash block appear between the intro blockquote and the
"Switching Voices Mid-Conversation" section, with clean ``` fences (no stray characters).

- [ ] **Step 3: Commit**

```bash
cd /home/wunsch/git/voicemode-local
git add CLAUDE.md
git commit -m "docs(voice): write a session label at voice-mode start

Tell Claude to write a short auto-derived label to
session_names/<CLAUDE_CODE_SESSION_ID>.txt so concurrent sessions announce
distinguishable names on handoff."
```

---

## Task 3: Manual verification (no code)

**Files:** none — verification only.

- [ ] **Step 1: Confirm the sidecar round-trips for the current session**

Run:
```bash
cd /home/wunsch/git/voicemode-local
mkdir -p ~/.voicemode/session_names
printf '%s' 'verify-label' > ~/.voicemode/session_names/"$CLAUDE_CODE_SESSION_ID".txt
VOICEMODE_SESSION_NAME= CLAUDE_CODE_SESSION_ID="$CLAUDE_CODE_SESSION_ID" \
  python -c "import sys; sys.path.insert(0,'patches'); import voice_queue; \
  print(voice_queue.session_project())"
```
Expected: prints `verify-label` (env empty → file used).

- [ ] **Step 2: Confirm env override still wins**

Run:
```bash
cd /home/wunsch/git/voicemode-local
VOICEMODE_SESSION_NAME=override-label \
  python -c "import sys; sys.path.insert(0,'patches'); import voice_queue; \
  print(voice_queue.session_project())"
```
Expected: prints `override-label`.

- [ ] **Step 3: Clean up the verification file**

Run: `rm -f ~/.voicemode/session_names/"$CLAUDE_CODE_SESSION_ID".txt`
Expected: no output (file removed). The real label will be (re)written by the CLAUDE.md
instruction at the next voice-mode start.

---

## Self-Review notes

- **Spec coverage:** fallback chain (Task 1 §4), env precedence (test_env_var_wins),
  file-by-SID (test_file_used), folder fallback (two tests), GC (test_gc + helper),
  CLAUDE.md instruction (Task 2), intro unchanged (explicitly out of scope, not touched),
  caller passes base (Task 1 §5). No converse signature change. All spec sections mapped.
- **Placeholders:** none — all code and commands are concrete. `<label>` in the CLAUDE.md
  block is an intentional template Claude fills at runtime, not a plan placeholder.
- **Type consistency:** `SESSION_NAMES_DIR` (str), `SESSION_NAME_MAX_AGE` (float),
  `session_project(base: Optional[Path])`, `_gc_session_names(base: Path)` used
  consistently across constants, function, tests, and caller. `Optional` and `Path` are
  already imported in `voice_queue.py`; `time` and `os` already imported.
```
