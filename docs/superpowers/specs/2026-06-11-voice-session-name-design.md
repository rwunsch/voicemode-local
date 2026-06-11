# Voice session names — design

**Date:** 2026-06-11
**Status:** Design (approved for planning)
**Related:** [voice session queue](2026-06-07-voice-session-queue-design.md)

## Problem

When two concurrent Claude Code sessions take FIFO turns on the voice channel, the
session that takes the floor after a wait announces itself with a spoken intro
(`patch_converse_queue.py`):

```
"This is {name}, {voice} — {message}"
```

Today `{name}` is `VOICEMODE_SESSION_NAME` (an env var almost never set) or, failing
that, the **cwd folder name** (`voice_queue.session_project()`). Two Claude sessions
open in the *same repo* therefore both announce `voicemode-local` and are
indistinguishable by voice.

We want a session to announce a **distinguishing label** — its "session name" — when it
has one, falling back to the folder name otherwise. The user phrased the goal as:
*"have Claude set `VOICEMODE_SESSION_NAME` based on the Claude session name."*

## Constraints (verified)

1. **The MCP server's environment is frozen at spawn time.** Claude Code launches the
   voice-mode server (`voicemode-mcp` → `voice-mode`) at session start. An
   `export VOICEMODE_SESSION_NAME=…` from a later Bash call does **not** reach the
   already-running server process. So "Claude sets the env var" cannot work through the
   env var at runtime.
2. **Claude Code does not expose a friendly session title to the agent.** The agent only
   has `CLAUDE_CODE_SESSION_ID` (a UUID). There is no API for the `/resume` summary title.
3. **The MCP server inherits its launching session's `CLAUDE_CODE_SESSION_ID`.** Verified
   by reading `/proc/<pid>/environ` of running `voice-mode` processes: each carries the
   session ID of the Claude Code session that spawned it, and concurrent sessions carry
   distinct IDs. This shared, per-session-unique value is the propagation key.

Because of (1)+(2), "based on the Claude session name" resolves to: **Claude chooses a
short label and writes it to a file keyed by the session ID the MCP server shares**, and
the MCP server reads that file as a fallback name source.

## Approach (chosen: sidecar file, set once)

Considered two wirings:

- **A — `converse` parameter.** Add `session_name` to the converse tool; pass it on every
  call. Rejected: couples to the converse signature (another upstream-drift patch anchor)
  and requires passing the param on every call.
- **B — sidecar file (chosen).** Claude writes the label once to
  `~/.voicemode/session_names/<CLAUDE_CODE_SESSION_ID>.txt`. `session_project()` gains a
  fallback chain. No converse change; set-and-forget; applies to all later calls.

### Components

**1. `voice_queue.session_project()` — fallback chain.**

```python
SESSION_NAMES_DIR = "session_names"
SESSION_NAME_MAX_AGE = 7 * 24 * 3600  # GC label files older than a week

def session_project(base: Optional[Path] = None) -> str:
    # 1. Explicit env override always wins (manual / launch-time naming).
    name = os.getenv("VOICEMODE_SESSION_NAME")
    if name and name.strip():
        return name.strip()
    # 2. Sidecar file keyed by the session id this MCP server inherited.
    sid = os.getenv("CLAUDE_CODE_SESSION_ID")
    if sid:
        base = Path(base) if base else DEFAULT_BASE
        f = base / SESSION_NAMES_DIR / f"{sid}.txt"
        try:
            label = f.read_text().strip()
            if label:
                return label
        except OSError:
            pass
    # 3. Folder name (current behaviour).
    return Path(os.getcwd()).name
```

- The env var keeps top precedence so a user who launches with it set, or sets it before
  spawn, still wins.
- File read is best-effort; any error falls through to the folder name. Never raises into
  the voice path.
- `session_project()` currently takes no args; adding an optional `base` keeps the
  existing zero-arg call sites working and lets tests point at a temp dir.

**2. Label-file cleanup (GC).** A small helper GCs `session_names/*.txt` whose mtime is
older than `SESSION_NAME_MAX_AGE`, called opportunistically (e.g. from `session_project`
or a write helper). Best-effort, swallow errors. Keeps the directory from accumulating one
file per historical session forever. (Optional nicety; tiny files, but cheap to do right.)

**3. CLAUDE.md instruction (voice-startup flow).** Extend the existing
"Voice Selection on Startup" section: when starting `/voicemode:converse`, Claude writes a
short session label so concurrent sessions are distinguishable on handoff.

- The label is **auto-derived by Claude** from what the session is about — a 1–3 word
  kebab-ish descriptor (e.g. `queue-naming`, `windows-port`) — falling back to the repo
  name if nothing specific stands out.
- Mechanism: one Bash line, e.g.
  `mkdir -p ~/.voicemode/session_names && printf '%s' '<label>' > ~/.voicemode/session_names/"$CLAUDE_CODE_SESSION_ID".txt`
- The user can override at any time by saying "call this session X"; Claude rewrites the
  file. (Switching is just another write — no restart needed, since `session_project()`
  reads the file fresh on each converse call via a new `QueueSession`.)

**4. Intro gating — no change.** `patch_converse_queue.py:187-190` already prepends the
intro only when `_q.waited` is true, i.e. only when the session actually waited behind
another holder. "Announce the name only if there are multiple sessions" is therefore
already satisfied; a solo session never speaks the intro.

### Data flow

```
Claude (voice start)
  └─ Bash: write ~/.voicemode/session_names/<SID>.txt = "queue-naming"

converse call (MCP server, same SID inherited at spawn)
  └─ QueueSession(project=None) → session_project()
       env VOICEMODE_SESSION_NAME? no
       → read session_names/<SID>.txt → "queue-naming"
  └─ acquire(): if it had to wait → intro = "This is queue-naming, Sky —"
```

## Error handling

- Missing / empty / unreadable label file → fall through to folder name (status quo).
- No `CLAUDE_CODE_SESSION_ID` (non-Claude-Code launch) → skip step 2, fall to folder name.
- Label-file write failure in Bash → non-fatal; session just keeps the folder name.
- GC failure → swallowed; never affects naming.

## Testing

Unit tests in `tests/` (extend `test_voice_queue.py` or a new `test_session_name.py`):

1. env var set → returned verbatim (trimmed), file ignored.
2. no env, file present (with a fake `base` + `CLAUDE_CODE_SESSION_ID`) → file contents.
3. no env, no file → folder name.
4. no env, empty/whitespace file → folder name.
5. no `CLAUDE_CODE_SESSION_ID` → folder name even if a file exists for some SID.
6. GC removes a label file older than `SESSION_NAME_MAX_AGE`, keeps a fresh one.

All use a temp `base`; no real `~/.voicemode` writes. Monkeypatch `os.getcwd` /
`os.environ` for determinism.

## Out of scope

- No change to the `converse` tool signature.
- No reading of the Claude Code `/resume` session title (not exposed to the agent).
- No change to queue arbitration, floor protocol, or the no-speech timeout.

## Files touched

- `patches/voice_queue.py` — `session_project()` fallback chain + GC helper + constants.
- `tests/test_session_name.py` (or extend `tests/test_voice_queue.py`).
- `CLAUDE.md` — voice-startup section: instruct Claude to write the label file.
- `docs/.../README.md` (optional work-doc per global instructions, post-implementation).
