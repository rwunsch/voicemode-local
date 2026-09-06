# Push-to-talk to the last session you spoke with — design

**Status:** design, not built. Written 2026-09-06 at Robert's request, to be discussed before any code.

## The ask

Several Claude Code sessions run at once. You press Ctrl+Space while none of them is
speaking, and the session you last talked to picks up the mic — so you can carry on a
conversation without going to find its window and type.

## What already works, and what doesn't

Today a PTT press reaches `~/.voicemode/control.sock`, which **only exists while a session
is inside a `converse` call**. So the behaviour splits cleanly:

| Session state | PTT press | Why |
|---|---|---|
| Speaking (TTS playing) | ✅ cuts it, opens the mic | verified 2026-09-06 |
| Listening (recording) | ✅ holds the mic open | verified 2026-09-06 |
| **Parked in `converse` awaiting your reply** | ✅ works | it *is* the listening case |
| **Idle at a text prompt** | ❌ nothing — no socket | this is the actual gap |

So the ask is narrower than it first looks. It is **only** about a session sitting idle at a
prompt, having finished its turn and returned to text.

That is worth saying because our own `CLAUDE.md` already instructs sessions never to end a
voice conversation on their own initiative — a well-behaved session should be parked in
`converse` and therefore already reachable. **The gap is mostly sessions that stopped
early**, which is arguably the thing to fix rather than to route around.

## What upstream gives us

Two of the three pieces exist:

- **`voicemode conch give <session>`** — hands the floor to a named session, and VM-1637
  extended it to summon *any running session*, not only one already queued.
- **Session identity** — our `patch_session_name` finally makes holders distinguishable;
  before it, every session reported as `converse`.

The third piece is the problem:

- **`notify_granted` nudges through tmux.** From `conch_notify.py`: a local grantee gets
  "a tmux pane nudge"; no `session` binary or no tmux is "a silent no-op". **Robert runs
  Claude Code directly in Windows Terminal, not tmux**, so upstream's push half is inert
  here. This is the load-bearing constraint and the reason this needs design rather than
  wiring.

## Missing state: who did I last speak with?

The conch records the *current* holder. Nothing records the *previous* one, and between
turns `~/.voicemode/conch` is cleared. So "the session I last spoke with" has to be
persisted by us — a small file written on each conch release:

```
~/.voicemode/last_holder.json
  {"session_id": "...", "agent": "upstream-realign", "pid": 12345,
   "project_path": "/home/wunsch/git/voicemode-local", "released": "<iso8601>"}
```

Cheap, and independently useful: `voicemode-switch queue` could show it.

## Three ways to wake an idle session

### A. tmux nudge (upstream's answer)
Reuse `notify_granted` as-is. **Requires running Claude Code inside tmux.**

- *For:* zero new mechanism, upstream already maintains it, works today for tmux users.
- *Against:* Robert does not use tmux, so it solves nothing here without changing how he
  works. Not obviously a bad trade — tmux would also make several other things easier —
  but it is a workflow change, not a feature.

### B. Synthesise keystrokes into the terminal window (Windows-side)
The PTT listener already runs on Windows and already knows the foreground window. It could
find the target session's terminal window and send it a keystroke to wake the agent.

- *For:* no workflow change; the listener is already there.
- *Against:* **fragile and a bit alarming.** Mapping a session id to a Windows window is
  guesswork (title matching, the same trap that already bit us once — Claude Code puts the
  session name in the title, which is why `◐ Voicemode` matched no terminal pattern). And a
  tool that types into windows is a footgun: pick the wrong window and you inject keystrokes
  into something else. I would want an explicit allow-list and a visible confirmation before
  building this.

### C. Keep sessions parked in `converse` (no new mechanism at all)
Lean on the rule we already have. A session that finishes speaking calls `converse` again
with `wait_for_response=true`, so it sits in the listen phase with the socket bound — and a
PTT press already works, today, with no new code.

- *For:* zero new machinery; it is the behaviour our `CLAUDE.md` already mandates; it is the
  only option that needs nothing built.
- *Against:* a parked session is blocked in a tool call and cannot do other work; several
  parked sessions contend for the conch; and it does not help a session that has already
  gone idle.

## DECIDED 2026-09-06: tmux is out

Robert rejected option A, and the reasoning is right: most users on Windows, macOS *and*
Linux do not run tmux, and **the voicemode MCP server is started by Claude Code itself** --
so requiring a terminal multiplexer wrapped around the agent is the wrong shape for a
feature meant to work out of the box. Option A is closed.

That also sharpens the upstream angle: `notify_granted`'s single tmux delivery path is not
merely inconvenient for us, it is unavailable to the majority of upstream's own users. The
gap is upstream's, not ours.

So the live options are C (keep sessions parked in `converse` -- no new code) and B
(Windows keystroke synthesis -- powerful but a footgun). Recommendation below stands with A
struck out.

## Recommendation

**C first, and B only if we decide the risk is worth it.** (A is ruled out, above.)

C is not a workaround — it is the design working as intended, and it costs nothing. Before
building anything, the honest first question is *why* sessions are idling at a prompt when
the instruction is to stay in the conversation. If the answer is "because they legitimately
finish", then:

- Add the `last_holder.json` record regardless — it is small, useful on its own, and a
  prerequisite for both A and B.
- Add `voicemode-switch ptt wake` that reads it and runs `conch give`. On tmux that
  completes the loop immediately via upstream's own notify. Off tmux it becomes a no-op
  that says so, rather than failing silently.
- Revisit B only with an explicit window allow-list, and only if the tmux route is rejected.

## Open questions for Robert

1. Would you consider running Claude Code inside tmux? It makes option A work immediately
   and is the path upstream maintains.
2. When a session goes idle at a prompt — is that sessions misbehaving against our
   never-end-the-conversation rule, or you deliberately ending turns? The answer changes
   which option is right.
3. If we do B, are you comfortable with a tool that synthesises keystrokes into a chosen
   window? I would want that opt-in and narrowly scoped.

## Upstream angle

"Wake an idle non-tmux agent" is a real gap in upstream's notify-on-give, not just ours —
`conch_notify` currently has exactly one delivery mechanism. Worth an issue once we know
what we actually want, and a natural companion to the hold-intent proposal on #312.
