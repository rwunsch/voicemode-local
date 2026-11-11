# Design: a real voice-queue callback on every harness

Status: approved in principle by Robert, 2026-11-11 (by voice); final read pending. Branch `feature/pill-autostart` (target v1.1.1),
on top of 33b9658 (`patches/conch_wakers.py`). Built in e5a2a65, e4a4902 and fb1a053.

Live result, 2026-11-11, WSL, two interactive Claude Code sessions (voicemode-local
and FELIX-voice). Source: `~/.voicemode/logs/session-wake.log` and `voicemode conch
status` polled every ~2 s. n=1 per row:

| Round | Case | Log (local time) | Result |
|---|---|---|---|
| 1 | callback queued behind a speaker, session idle at release | `16:40:13 reason=conch ok=True detail=sent` | woken, spoke |
| 2 | same | `16:43:13 ok=True detail=sent` | woken, spoke |
| 3 | session busy at release | `16:44:05 ok=False detail=busy` | place kept; it took its own turn ~1 min later |
| 4 | session busy; a free-mic caller after 24 s unclaimed | `16:49:19` and `16:49:51 ok=False detail=busy` | the caller spoke past the grant; at its release the grant came back with a fresh clock and the session was woken again |

Silent live rounds, 2026-11-11 19:08-19:11, WSL. A script played the other
sessions through `Conch`/`ConchQueue` from the voice server's venv. The real
callback waiter was this Claude Code session, kept busy throughout. Source: the
script's output plus `session-wake.log`. n=1 per row:

| Case | Result |
|---|---|
| 5-wake cap, 6 grant events | 5 `ok=False detail=busy` lines (19:08:35-19:09:24), then "woken 5 times for this turn; not again"; `conch.wakes.json` count 6 for seq 61 |
| present caller passes an absent grant | 6 of 6 cycles got the mic (12 s after each grant, past the grace) |
| `conch give`, present caller | held off 30.3 s (grant TTL), then served |
| wait waiter queued 1 s after a callback grant (gap 1) | got the mic 10.3 s after the grant (grace) |

Defects found by these rounds, not yet fixed:
- **A give is sticky.** The re-stamp in `grant_next` rewrites the whole grant file, so
  `"given": true` survives every later release, and each cycle holds for 30 s instead
  of the 10 s grace. In the first run, phase C waited 30.4 s behind a give left over
  from phase B.
- **Double ping on a pass.** `_current_grant`'s present-beats-absent branch pings the
  callback session when a wait waiter takes its grant (19:11:32.98), and the waiter's
  release pings it again (19:11:33.00). The first ping says "your turn" while
  another session is about to speak, and it uses up one of the 5 wakes.

Both fixed in 2abfeff. Re-run 19:47-19:49 (n=1): the wait waiter passed at 10.1 s
after a spent give, and the pass sent no ping.

Fair holds and progress (2e515a5), live on WSL with Claude Code, 2026-11-11 20:06
(n=1 each; source: `~/voicemode_debug.log` and a watcher script):
- Claude Code sends a progress token: "progress heartbeat: every 30s, client token present".
- A `hold_conch=true` turn with no reply, while a test session waited: "hold released,
  no reply this turn and someone is waiting". The waiter got the mic at once, and no
  held marker appeared.
- 3-turn cap, 21:28-21:31 with Robert answering: held after turns 1-3, then "hold released
  after 3 held turns, someone is waiting" and the test session got the mic (n=1).
  Turn 3's transcript was only dots and was counted as a reply; fixed afterwards: only
  a transcript with a letter or digit counts (`conch_hold.is_reply`).
- No-reply release seen 3 times (20:06, 21:02 twice).
- Not live yet: progress in Copilot.

Copilot wake measurement (evidence: FELIX-voice's runs on Robert's laptop, Copilot app
runtime 1.0.106-8, a test extension calling `session.send`; n=1 each, not re-run here):
- Idle chat (~1.5 min): woken, turn started +7.4 s after the send.
- Idle 12 min 18 s: not unloaded, woken +14 s. The only unload seen was ~37 min after
  load (idle time unknown).
- Busy chat or open question: the send is accepted and queued and runs after the turn
  ends. It never reports busy, so the bridge must return 75 itself, from the
  extension's `session.idle` events, and keep at most one pending wake per chat.
So the Copilot route (wake command → loopback → extension → `session.send`) is viable;
the bridge is FELIX's to build.

Not tested live: any harness other than Claude Code on WSL.
Seen during the test: `conch status` still says "Every other waiter is blocked
behind this grant" past the grace (deferred minor).

Open (Robert, 2026-11-11): a generic wake path through MCP that works for Copilot,
Claude Code and others. It would be added beside the Claude Code waker and would not
replace it. Spike report from FELIX-voice, 2026-11-11 (evidence: its agent's runs, n=1
each, not re-run here; Copilot CLI 1.0.107, Claude Code 2.1.322):
- **No standard MCP message from a stdio server started a model turn**, during or
  after a tool call. That covers sampling, elicitation, logging, resources/updated,
  tools/list_changed and progress. Sampling in Copilot is a separate model call that
  does not include the chat's history.
- Claude Code `notifications/claude/channel` exists (research preview; needs a flag
  and, for Team/Enterprise, `channelsEnabled`). Nothing arrived in print mode (n=2),
  cause unknown. Copilot has no counterpart.
- Copilot app: extension `session.send` (enqueue/immediate) exists and FELIX already
  uses it. Whether it wakes an **idle** chat is unmeasured (n=0). The app unloads
  idle chats after about 10 min (read in FELIX research).
- Proposed next measurement: does `session.send` from the chat's FELIX extension
  wake an idle chat, and what happens after the unload. If it does, the route is:
  voice server → `VOICEMODE_CONCH_WAKE_COMMAND` → loopback → that chat's extension →
  `session.send`. Until then, callback stays served as wait in Copilot.

## Intent (Robert, 2026-11-11, by voice)

Several sessions share one voice channel. A session that is waiting for its turn **keeps
working** instead of blocking. When it has something to say it joins the queue. Whoever is
talking **releases the mic after each turn**, unless it is in a really important
back-and-forth. The next session in line **says its name, then talks**. A session that went
quiet or stopped while queued is **woken when its turn comes** and picks up where it left
off. A session that is **still busy when its turn comes is skipped**: the others talk, it
keeps its place, and it answers only when it has something to say.

It must work on every harness: Claude Code (WSL, Linux, Mac, native Windows terminal), the
Copilot app (FELIX on Windows and Mac), and tmux users.

## What already exists (searched 2026-11-11: repo, docs, git history)

| Robert's rule | Where it already lives | State |
|---|---|---|
| Waiting session keeps working | Upstream 8.12 `conch_mode="callback"`: converse returns at once, the session stays queued | Built upstream |
| Release after each turn, hold for an important discussion | Upstream default release + `hold_conch=true` (10 s idle TTL, refreshed per turn) | Built, documented in CLAUDE.md |
| Next session says its name | `patch_session_name.py`: the conch names sessions; the label is spoken on handoff | Built |
| A busy (callback) session is skipped while someone is waiting | Upstream `ConchQueue.grant_next` (VM-1625 F1): leading callback waiters are skipped when a wait waiter exists, and each skipped one is pinged | Built upstream |
| A skipped session keeps its place | Upstream `register` is idempotent per session id: the same `seq` | Built upstream |
| Wake the session when its turn comes | Upstream `conch_notify.notify_granted`: tmux only. Ours: `conch_wakers.py` (33b9658) = harness command, then Claude Code's session socket (`session_wake.py`, POSIX), then tmux; with none, callback is served as wait | Built today; Claude Code path validated only as far as "the waker finds the session" (n=1) |

History: our own FIFO queue (`voice_queue.py`, June 2026, 50 s re-call slices) was replaced
by upstream's conch on 2026-09-05. Waking without tmux was designed 2026-09-06
(`2026-09-06-ptt-wake-last-session-design.md`); Robert ruled tmux out because most users on
Windows, macOS and Linux do not run it and Claude Code starts the voice server itself. That
led to `session_wake.py` (2026-11-07, push-to-talk and the pill's Wake).

**Why tmux cannot be the answer** (asked again 2026-11-11): it reaches only a terminal
session started inside tmux (Linux, WSL, Mac by opt-in). Native Windows has no tmux, and
the Copilot app is a desktop window, so tmux can never reach FELIX. It stays as the last
fallback in the waker chain.

## What is missing (the work)

### Gap 1: a late waiter is stuck behind a callback grant (seen in FELIX, 2026-11-11, n=1)

`grant_next` skips callback waiters only if a wait waiter **already** exists at release
time. In FELIX, chat 2 released while chat 1 (callback) was the only waiter, so chat 1 was
granted. Chat 2 queued again 7 s later in wait mode, and `Conch._queue_grant_blocks` held
it behind chat 1's grant, which nothing claimed. The queue stood still.

**Fix:** a standing grant that names a **callback** waiter does not block a **wait** waiter.
When a wait waiter polls (`try_acquire`) and the grant names a callback waiter, promotion
runs again by upstream's own rule: the first wait waiter is granted, and the callback
waiter stays in line and is pinged again. Exception: an explicit `conch give` to a callback
waiter stands (operator intent), so a give must be recognisable in the grant file.

### Gap 2: a woken session that is busy

`session_wake` refuses a session that is `busy` or `waiting` (permission prompt). Robert's
rule: a busy session is skipped and answers only when it has something to say. With gap 1
fixed, a refused wake can no longer block anyone: the grant stays on the busy session only
while nobody else waits, and the session claims it the next time it calls converse. A
session that **finishes work and goes idle** without calling converse has missed its ping.
It must be pinged again: at the latest on the next grant event that grants it (amended
2026-11-11, 2abfeff: a waiter that is skipped is no longer pinged, because our wake says
"your turn has come"; it is woken when a grant names it), and, while the grant still names it, again when its Claude Code
status turns `idle` (cheap: re-try on the waiter's own heartbeat, rate-limited by
`session_wake`'s 5 s limit).

### Gap 3: Copilot (FELIX) has no waker

The seam exists: `VOICEMODE_CONCH_WAKE_COMMAND` (environment only; session id and voice
server pid appended; exit 0 = delivered). FELIX would point it at a command that makes its
in-chat app extension call `session.send({mode: "enqueue"})` (reported by FELIX-voice from
the SDK types; that it wakes an idle chat is unproven, n=0). **FELIX-voice builds and
proves this in felix-framework.** Until it is proven, FELIX stays on "callback is served as
wait".

## Goals

- No queue state can leave the mic unusable while a live session wants it (gaps 1 and 2).
- A callback session in Claude Code on POSIX is woken in practice, proven live with two
  real sessions (n≥3 round trips).
- The same behaviour on every harness; where a harness has no waker, callback is wait.

## Non-goals

- tmux as a requirement.
- A Copilot waker inside voicemode-local (that's FELIX's extension; we provide the seam).
- Changing how wait mode or names work. (Hold was a non-goal; amended 2026-11-11 at
  Robert's request, 2e515a5: while another session is **blocked waiting** (wait mode), a
  held turn with no reply releases and at most 3 held turns in a row keep the mic. A
  speak-only held turn counts toward the 3 but is not "no reply". A busy callback
  session does not count as waiting: it keeps working.)
- Native Windows Claude Code waking, until `session_wake`'s named-pipe path is verified
  (separate check).

## Anti-goals (a failure even if it technically works)

- Two sessions talking at once.
- A session losing its place in line because it was busy.
- A woken session speaking something stale or out of context: the wake text tells the
  model its queued message was not spoken; it decides what to say.
- Waking a session that is waiting on a permission prompt.

## Constraints

- Patches stay anchor-checked against pristine 8.12.0, idempotent, CRLF-safe; the WSL/Mac
  and native manifests both carry them; `conch_queue.py` / `conch.py` join `unshare`.
- Never break the critical path: every new branch fails safe to upstream behaviour.
- `session_wake` rules stay as they are (idle/shell only, private socket checks, fixed
  messages, rate limit).
- Upstream may take gap 1 as a PR later; keep the patch small and self-contained.

## Calls made after the builder check (mine unless marked)

1. **A woken session may say nothing.** The `conch` wake text is reworded, from "say your
   queued message" to "your turn has come: speak if you still have something to say, else
   do nothing". It is our own message, so rewording it is fine. A session that stays silent
   keeps its place, and because of the gap 1 fix it blocks nobody. Pings are capped at **5
   per grant** (`VOICEMODE_CONCH_MAX_WAKES`, default 5): one when granted, then one per
   later grant event. No endless re-pings. (Robert, 2026-11-11: silence is fine; two wakes
   are not enough, five.)
2. **"Present beats absent."** Generalising gap 1: a grant that names a callback session
   which has not come back does not block anyone who actually calls converse now. Among
   present callers, queue order holds. A caller with `wait_for_conch=false` and a free mic
   is served. A session that was just woken gets a **grace of 10 s** to come back before
   present callers may pass it. (Robert, 2026-11-11: agreed, 10 s rather than 15.)
3. **`conch give`** marks its grant `"given": true`. A given grant to a callback session
   holds against present callers for `CONCH_GRANT_TTL` (30 s), then yields like any other.
   An old grant file without the marker is read as "not a give".
4. **No new background timer** for a lone busy session that later goes idle. By Robert's
   rule a busy session answers when it has something to say; it calls converse itself and
   finds its grant. It is pinged again only on the next grant event.
5. **Wake command result codes:** exit 0 = delivered; exit 75 (EX_TEMPFAIL) = busy or
   permission prompt, so it counts as refused and may be pinged again on the next grant
   event; any other code = cannot reach. FELIX-voice is told before it builds.

## Left to the builder

- Whether gap 1 lives in `Conch._queue_grant_blocks`, `ConchQueue._current_grant`, or both,
  and how a give is marked (e.g. `"given": true` in `conch.grant`, written by `grant()`).
- How gap 2's idle re-ping is triggered (heartbeat hook vs the next `grant_next`), within
  the rate limit.
- Test shape; the live test uses two headless or interactive Claude Code sessions on WSL
  (`docs/testing/multi-session-voice-test.md` has a self-executed variant).
