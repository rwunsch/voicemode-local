# Voice Session Queue — Design

**Date:** 2026-06-07
**Status:** Approved by user (brainstorming session)
**Scope:** voicemode-local patch overlay on voice-mode; portable to Windows-native

## Problem

Multiple concurrent Claude Code sessions use voicemode on one machine, each with a
distinct voice so the user can track context by ear. Today, coordination relies on
voice-mode's `conch.py` (a single fcntl flock at `~/.voicemode/conch`), and it fails
in practice:

1. `wait_for_conch` defaults to `false` — a blocked session gets a text reply
   ("User is currently speaking with X, try again later") and whether it retries
   depends on the LLM. Questions get lost ("go under").
2. Waiting is unfair polling, not a queue — every waiter retries `try_acquire()` on
   a 0.5s timer; whoever polls luckiest wins. No FIFO, waiters can starve.
3. The lock is per-`converse`-call — another session can snatch the floor between
   two exchanges of an ongoing conversation.
4. Waiting times out after 60s (`CONCH_TIMEOUT`) with another easily-ignored text
   message.
5. No identity — `agent_name` is hardcoded `"converse"`; the user cannot be told
   which session is waiting or speaking.
6. `fcntl` is POSIX-only, blocking the planned Windows-native port (see
   `docs/windows-issues.md` issue #1 in the Windows working copy).

## Goal

An **explicit, fair, named queue**: concurrent sessions take strict FIFO turns;
a session keeps the floor for a natural conversation burst; queued questions are
never lost no matter how long the wait; each handoff announces who is speaking.

## Requirements (user-confirmed)

| Decision | Choice |
|---|---|
| Turn length | **Conversation burst** — floor held across immediate follow-up exchanges, released when the session has nothing more to ask |
| Queue awareness | **Handoff intro only** — waiting sessions are silent; on acquiring the floor after a wait, the session opens with "This is \<project\>, \<voice\> —" |
| Long waits | **Wait indefinitely** — queue position is preserved until the turn comes, however long |
| Identity | **Auto** — project directory name + voice |
| Approach | **A: ticket-file queue (patch overlay)**; broker daemon (B) is the documented fallback if file-based coordination proves flaky |
| Portability | Mechanism must work unchanged on Windows (no fcntl) |

## Architecture

All coordination state lives on the shared filesystem — the only meeting point of
the independent voice-mode MCP server processes (one per Claude Code session):

```
~/.voicemode/
├── queue/                           # FIFO of waiting sessions
│   └── <epoch-ms zero-padded>-<pid>.json   # one ticket per waiting session
└── floor.json                       # current floor holder ("talking stick")
```

### Tickets

- Filename `001749301234567-41023.json`: zero-padded epoch-milliseconds makes
  lexicographic order == chronological order; PID suffix guarantees uniqueness.
- Created with `O_CREAT|O_EXCL` — atomic on Linux/macOS/Windows. **No flock
  anywhere in the design.**
- Content: `{ "pid", "project", "voice", "created" }`.
- **Head of queue** = oldest ticket whose PID is alive. Dead-PID tickets are
  garbage-collected by any process that notices them.

### Floor

- `floor.json`, written atomically (temp file + `os.replace`).
- Content: `{ "pid", "project", "voice", "acquired", "last_activity" }`.
- `last_activity` is heartbeat-updated by the holder during activity (TTS start,
  recording start, recording chunks, exchange end). Active exchanges can run
  arbitrarily long without the floor becoming stealable.
- Floor is **dead** when: holder PID no longer exists (instant detection), or
  `last_activity` older than the grace period (default 45s).
- Acquisition: only the head-of-queue waiter attempts it. If the floor file is
  absent or dead, claim via `O_EXCL` create (after removing a dead file). If two
  processes ever race the claim, `O_EXCL` picks exactly one winner.

### PID liveness

`pid_alive(pid)` helper: `os.kill(pid, 0)` on POSIX; `ctypes`/`OpenProcess` on
Windows. Together with `O_EXCL`, this is the entire portability surface.

## Converse protocol

`converse` gains two parameters: `ticket` (string, optional) and `end_burst`
(bool, default false).

```
converse called
│
├─ This process already holds the floor (burst continuation; detected by
│    floor.json pid == os.getpid()):
│    speak/listen normally, heartbeat last_activity.
│    If end_burst=true: after the exchange, delete floor.json.
│
└─ Floor not held:
   ├─ Ensure ticket exists (reuse `ticket` param if given — preserves FIFO
   │  position across re-calls; create new ticket otherwise)
   ├─ Wait loop (max WAIT_SLICE ≈ 50s per call, poll every 0.5s):
   │    floor dead/free AND we are head?  → acquire floor, DELETE own ticket
   │    (the floor file itself is the record of holding; a holder never sits
   │    in the queue, so "head" always means the oldest *waiting* session)
   │      ├─ acquired after waiting   → prepend handoff intro to TTS
   │      └─ acquired instantly       → no intro (queue was empty)
   └─ Still waiting at 50s → return structured QUEUED status (not an error):
        "QUEUED — position N of M. The user is speaking with <project>/<voice>.
         You MUST immediately call converse again with the same message and
         ticket=<id>. Your queue position is preserved. Do NOT print your
         question as text. Do NOT give up."
```

### Why the 50s slice + mandatory re-call

MCP clients (including Claude Code) impose tool-call timeouts; a call blocked for
many minutes risks being killed, losing the question — the original bug. Slicing
keeps every individual call short while the **ticket** preserves queue position
across any number of re-calls. Each QUEUED return also re-states the protocol in
the tool result itself, so even a context-degraded LLM is re-told what to do.
"Wait indefinitely" is therefore a property of the ticket, not of any single call.

### Burst release (two-tier)

1. **Explicit:** the LLM passes `end_burst=true` on its final exchange
   (instructed by the patched prompt and CLAUDE.md).
2. **Grace timeout:** if `last_activity` is older than `VOICEMODE_QUEUE_GRACE`
   (default 45s), the head waiter declares the floor stale and claims it. 45s is
   longer than realistic LLM thinking-time between burst exchanges, but bounds the
   damage of a forgotten `end_burst` to ~45s. Grace measures only silence
   *between* exchanges — never an in-progress exchange (heartbeats cover those).

### Session death

Holder process exits/crashes → PID dead → floor free on the next waiter poll.
No grace wait, no stale-lock window (replaces conch's 300s expiry).

## Identity, visibility, config

- **project** = `basename(os.getcwd())` of the MCP server process (Claude Code
  spawns it with the project as cwd — verify during implementation), overridable
  via `VOICEMODE_SESSION_NAME`. **voice** = the call's `voice` param.
- **Handoff intro**: spoken in the session's own voice, exactly once per floor
  acquisition that followed a wait: "This is \<project\>, \<voice short name\> —"
  (voice short name derived from the voice id, e.g. `af_bella` → "Bella").
  Never repeated within a burst.
- **`voicemode-switch queue`**: read-only subcommand printing floor holder, queue
  order with ages, and cleaning dead tickets.
- **Env config** (all optional):
  - `VOICEMODE_QUEUE_ENABLED` (default `true`; `false` restores old conch behavior)
  - `VOICEMODE_QUEUE_GRACE` (default `45`)
  - `VOICEMODE_QUEUE_WAIT_SLICE` (default `50`)
  - `VOICEMODE_QUEUE_CHECK_INTERVAL` (default `0.5`)
  - `VOICEMODE_SESSION_NAME` (default: cwd basename)

## Patching strategy

- **`patches/voice_queue.py`** — new module copied to
  `voice_mode/voice_queue.py` by `patches/apply.sh` (like the Windows shims): no
  upstream file collision. Pure stdlib; unit-testable without audio.
- **`tools/converse.py` integration** — a surgical, pattern-anchored patcher in
  `apply.sh` (Python-based) that locates the existing conch arbitration block and
  replaces it with queue calls, plus inserts heartbeat hooks at the TTS/record
  phases. If upstream changed and a pattern does not match, the patcher **fails
  loudly** — never a silent half-patch. We do NOT ship a full copy of
  `tools/converse.py` (~2,100 lines of drift risk).
- **Prompt + CLAUDE.md** — LLM contract (kept deliberately tiny, two parameters
  and two rules): re-call immediately on QUEUED with the ticket; pass
  `end_burst=true` on the final exchange; never degrade a queued question to text.
- **Old `Conch`** — no longer arbitrates. Check during implementation whether
  anything external reads `~/.voicemode/conch` (e.g. sound-effect hooks); if so,
  keep writing it for compatibility while the floor is held.
- Windows later: `install.ps1` Step 4 must run the same patch set (its current
  failure to invoke `apply.sh`'s shim installs is a known bug from the
  2026-06-07 review).

## Error handling

- Corrupted/unparsable ticket or floor JSON → treated as dead, cleaned up by
  whoever reads it.
- `ticket` param refers to a file that no longer exists (e.g. cleaned up after a
  transient PID-liveness misread) → create a fresh ticket at the back and say so
  in the next QUEUED status.
- Queue dir missing → created on demand.
- Wall-clock changes are harmless: ordering is fixed at filename creation;
  positions never re-sort.

## Testing

- **Unit** (joins existing `tests/`): FIFO ordering; `O_EXCL` contention with real
  subprocesses racing for the floor; dead-PID cleanup; grace expiry; burst
  hold/release; ticket reuse across re-calls; corrupted-JSON recovery.
- **Integration**: two scripted fake sessions driving `voice_queue` end-to-end,
  asserting strict turn order and intro-exactly-once.
- **Manual acceptance**: 2–3 real Claude Code sessions; ask each for something;
  walk away mid-queue; return — every question must arrive, in order, each with
  its handoff intro.

## Fallback plan

If file-based coordination proves flaky in practice (e.g. surprising filesystem
semantics, polling latency), fall back to **Approach B**: a tiny local HTTP
queue-broker daemon (like whisper-proxy, port ~2025) owning the FIFO, with
converse calling acquire/heartbeat/release endpoints. The converse-side protocol
(tickets, QUEUED re-call, bursts, intros) stays identical — only the storage
backend changes.

## Out of scope

- Windows-native port itself (separate spec; this design deliberately avoids
  fcntl so the queue ports unchanged).
- Upstreaming to voice-mode as a "conch v2" PR — desirable follow-up once proven
  locally.
