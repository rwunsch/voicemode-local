# Voice Session Queue — Design

**Date:** 2026-06-07
**Status:** Approved by user (brainstorming session); hardened after external review (Cursor, 2026-06-07)
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
│   └── <epoch-µs, 16-digit>-<pid>.json   # one ticket per waiting session
└── floor.json                       # current floor holder ("talking stick")
```

The queue directory must live on a **local disk** (atomicity guarantees below do
not hold on SMB/NFS); `~/.voicemode` satisfies this on all target setups.

### Process identity

Liveness checks use **pid + process start-time** (never pid alone — PIDs are
recycled by the OS and a recycled pid would make a dead session's ticket
immortal):

- `process_identity(pid)` → start-time: `/proc/<pid>/stat` field 22 on Linux,
  `GetProcessTimes` via ctypes on Windows.
- `pid_alive(pid, start_time)`: `os.kill(pid, 0)` on POSIX
  (**`PermissionError` ⇒ alive**), `OpenProcess` via ctypes on Windows; then
  start-time must match.

### Tickets

- Filename `1749301234567890-41023.json`: zero-padded 16-digit epoch-microseconds
  makes lexicographic order == chronological order; PID suffix guarantees uniqueness.
  (Wall-clock steps backward could let a new ticket sort earlier — accepted as
  negligible on a single-user desktop; positions never re-sort after creation.)
- Created with `O_CREAT|O_EXCL` — atomic on Linux/macOS/Windows. **No flock
  anywhere in the design.**
- Content: `{ "pid", "start_time", "project", "voice", "created", "last_seen" }`.
- **One ticket per PID**: ticket creation first deletes any existing tickets with
  the same pid (prevents the orphan-ticket deadlock when an LLM re-calls without
  the `ticket` param). Head determination compares pid, not filename.
- **Ticket heartbeat**: the owner updates `last_seen` (atomic temp +
  `os.replace`) on every poll of its wait loop. Any process may GC a ticket
  whose pid+start_time is dead **or** whose `last_seen` is older than
  `TICKET_STALE` (default 30s — covers abandoned tickets whose LLM never
  re-called; an over-slow re-call merely re-enqueues at the back, the question
  itself is not lost).
- **Head of queue** = oldest non-stale ticket. With an empty queue, a new ticket
  is trivially head and the floor is acquired instantly (no intro).
- The `ticket` tool parameter is the **filename stem** (e.g.
  `1749301234567890-41023`). If it no longer exists, a fresh ticket is created at
  the back and the next QUEUED status says so.

### Floor

- `floor.json`. Content: `{ "pid", "start_time", "project", "voice", "acquired",
  "last_activity" }`.
- Floor is **dead** when: holder pid+start_time no longer matches a live process
  (instant detection), or `last_activity` is older than `QUEUE_GRACE`
  (default 90s).
- **Claim protocol** (only the head waiter attempts it):
  1. If a `floor.json` exists and looks dead: atomically
     `os.rename(floor.json, floor.stale.<uuid>)`, then re-verify the renamed
     snapshot. If it was actually live (heartbeat raced the read), rename it
     back and resume waiting. If dead, delete the snapshot.
  2. Claim: write the full new floor content to a private temp file, then
     `os.link(tmp, floor.json)` — atomic fail-if-exists **with complete
     content** (readers can never observe an empty/partial floor). Works on
     ext4 and NTFS (same volume). `FileExistsError` ⇒ lost the race, resume
     waiting.
  3. On success: delete own ticket. **The floor file is the record of holding;
     a holder never sits in the queue**, so "head" always means the oldest
     waiting session.
- **Conditional heartbeats**: before each `last_activity` update, the holder
  re-reads `floor.json`; if `pid != me`, the floor was stolen (grace expiry) —
  the holder demotes itself: it is no longer in a burst, must not write
  heartbeats, and re-queues with a new ticket on its next converse call.
  A microsecond-scale write race remains theoretically possible; it can at
  worst cause one overlapping utterance and self-heals on the next call —
  accepted for a human-timescale voice protocol.
- Heartbeat points: burst-continuation entry (every converse call while
  holding), plus a **call-scoped background asyncio task** that heartbeats every
  ~10s for the duration of each converse call (covers TTS and recording with a
  single injection point instead of per-phase hooks), cancelled when the call
  ends. Between calls there are no heartbeats — grace measures exactly the
  inter-call silence.

### Intra-process concurrency

Claude Code can issue parallel tool calls; `converse` is async. All ticket/floor
mutations go through one **process-local `asyncio.Lock`** in `voice_queue.py`, in
addition to the cross-process file protocol.

## Converse protocol

`converse` gains two parameters: `ticket` (string, optional) and `end_burst`
(bool, default false). When the queue is enabled, the legacy `wait_for_conch`
parameter is **ignored** (queue semantics always apply); it remains meaningful
only under `VOICEMODE_QUEUE_ENABLED=false`.

```
converse called
│
├─ This process already holds the floor (floor.json pid+start_time == me):
│    heartbeat last_activity, speak/listen normally (burst continuation).
│    If end_burst=true: after the exchange, delete floor.json.
│
└─ Floor not held:
   ├─ Ensure ticket exists (reuse `ticket` param if its file still exists —
   │  preserves FIFO position across re-calls; else create new, deleting any
   │  same-pid leftovers)
   ├─ Wait loop (max WAIT_SLICE ≈ 50s per call, poll every 0.5s,
   │  heartbeat own ticket's last_seen each poll):
   │    floor dead/free AND we are head?  → run claim protocol
   │      ├─ acquired after waiting   → prepend handoff intro to TTS
   │      └─ acquired instantly       → no intro (queue was empty)
   └─ Still waiting at 50s → return structured QUEUED status (not an error):
        "QUEUED — position N of M. The user is speaking with <project>/<voice>.
         You MUST immediately call converse again with the same message and
         ticket=<id>. Your queue position is preserved. Do NOT print your
         question as text. Do NOT give up."
        (N of M is computed from a directory scan and is approximate under
         concurrent GC — cosmetic only.)
```

### Why the 50s slice + mandatory re-call

MCP clients (including Claude Code) impose tool-call timeouts; a call blocked for
many minutes risks being killed, losing the question — the original bug. Slicing
keeps every individual call short while the **ticket** preserves queue position
across any number of re-calls. Each QUEUED return also re-states the protocol in
the tool result itself, so even a context-degraded LLM is re-told what to do.
"Wait indefinitely" is therefore a property of the ticket, not of any single
call. Residual risk: an LLM that ignores the instruction entirely; its ticket
then ages out via `TICKET_STALE` instead of blocking the queue, and the failure
is visible in `voicemode-switch queue`.

### Burst release (two-tier)

1. **Explicit:** the LLM passes `end_burst=true` on its final exchange
   (instructed by the patched prompt and CLAUDE.md).
2. **Grace timeout:** if `last_activity` is older than `VOICEMODE_QUEUE_GRACE`
   (default **90s** — sized to cover slow LLM generation between exchanges),
   the head waiter runs the claim protocol. Grace measures only silence
   *between* exchanges — never an in-progress exchange (heartbeats cover those).

**Intended semantics, not a bug:** a session that goes quiet longer than grace
(e.g. runs tests for 5 minutes mid-conversation) *yields the floor by design* —
other sessions get their turns; the slow session re-queues with a fresh intro
when it returns. A forgotten `end_burst` jams the queue for at most ~90s.

### Session death

Holder process exits/crashes → pid+start_time dead → floor free on the next
waiter poll. No grace wait, no stale-lock window (replaces conch's 300s expiry).

## Identity, visibility, config

- **project** = `basename(os.getcwd())` of the MCP server process (Claude Code
  spawns it with the project as cwd — verify during implementation), overridable
  via `VOICEMODE_SESSION_NAME`. **voice** = the call's `voice` param.
- **Handoff intro**: spoken in the session's own voice, exactly once per floor
  acquisition that followed a wait: "This is \<project\>, \<voice short name\> —"
  (voice short name derived from the voice id, e.g. `af_bella` → "Bella").
  Never repeated within a burst.
- **`voicemode-switch queue`**: read-only subcommand printing floor holder, queue
  order with ages and staleness, and cleaning dead tickets.
- **Env config** (all optional):
  - `VOICEMODE_QUEUE_ENABLED` (default `true`; `false` disables cross-session
    arbitration entirely — the legacy conch path is not preserved, since keeping
    both code paths would double the surgical-patch surface)
  - `VOICEMODE_QUEUE_GRACE` (default `90`)
  - `VOICEMODE_QUEUE_WAIT_SLICE` (default `50`)
  - `VOICEMODE_QUEUE_CHECK_INTERVAL` (default `0.5`)
  - `VOICEMODE_QUEUE_TICKET_STALE` (default `30`)
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
  mirror it best-effort while the floor is held (readers may briefly observe
  conch and floor out of sync — acceptable, both are advisory for externals).
- Windows later: `install.ps1` Step 4 must run the same patch set (its current
  failure to invoke `apply.sh`'s shim installs is a known bug from the
  2026-06-07 review).

## Error handling

- Corrupted/unparsable ticket or floor JSON → treated as dead, cleaned up by
  whoever reads it (the link()-based claim guarantees a *valid* floor is never
  observed partially written, so unparsable ⇒ genuinely broken).
- `ticket` param refers to a file that no longer exists → create a fresh ticket
  at the back and say so in the next QUEUED status.
- Queue dir missing → created on demand.

## Testing

- **Unit** (joins existing `tests/`): FIFO ordering; claim contention with real
  subprocesses racing for the floor; dead-PID and pid-reuse (start-time
  mismatch) cleanup; grace expiry; **TOCTOU steal vs concurrent heartbeat**
  (holder heartbeats while waiter runs claim protocol — exactly one holder
  survives); **duplicate same-pid tickets** (re-call without ticket param must
  not deadlock); burst hold/release; ticket reuse across re-calls; ticket
  staleness GC; corrupted-JSON recovery.
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
