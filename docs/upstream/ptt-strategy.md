# Push-to-talk: strategy before code

**Status:** decision pending. **Do not open a PR yet.**

## The landscape

- **#312** "Support for push-to-talk and/or interruptible converse mode" — open since 2026-03-11.
- **#93** "Feature Request: Push to Talk" — closed.
- **#328** "hold-mode PTT with TTS interrupt, tag-based polyglot TTS, and lo..." — **an open PR
  since 2026-03-27**, unmerged for 5+ months.
- **8.11.0 shipped a control channel** we did not have when we designed ours.

## The thing that changes our plan

8.11.0's control channel already provides most of PTT's plumbing: a local-only socket into the
running server, `pause` / `resume` / `stop`, and crucially **`skip-forward`**, documented as:

> pressed while the assistant speaks, it cuts the utterance and hands you the mic; pressed while
> *you* speak, it ends the recording immediately and transcribes what was captured — a manual
> end-of-turn and a reliable VAD fallback. Lands in ~200 ms.

That is barge-in plus manual end-of-turn — two of the three things our PTT patch implements. It
is already hardened (VM-1688: peer-credential auth, `0700` socket dir, bounded input) and already
driven by media keys and Stream Deck.

**Implication:** our PTT should probably be re-expressed as a **control-channel client** rather
than a `converse.py` monkey-patch. Concretely, the key listener sends named intents over the
existing socket instead of us patching the recording loop. That would:

- survive every future upgrade (no anchors to drift — and note `converse.py` went 2261 → 4620
  lines in five releases, breaking both our PTT patchers);
- reuse upstream's hardening rather than duplicating it;
- reduce the PR from ~2,000 lines to a listener plus a `hold` / `release` intent pair, which is
  a size a maintainer at 5% merge rate might actually review.

**What is genuinely still missing upstream:** a *hold* semantic. `skip-forward` is an edge
trigger; PTT needs level-triggered "recording is open exactly while the key is down", plus the
press-to-barge-in-and-start-talking combination. That is the real contribution.

## What we have

`feature/push-to-talk`: 17 commits, 1,984 lines, 9 test files — press/hold/release state machine,
TCP relay bus, X11 listener, Windows listener, WSL2 companion, barge-in on press, terminal focus
scoping. Built against 8.7.1; both patchers (`patch_converse_ptt.py`, `patch_core_ptt.py`) will
drift on 8.12.0.

## Recommended sequence

1. **Read #328 properly** before writing anything:
   ```bash
   gh pr view 328 -R mbailey/voicemode --json title,body,comments
   gh pr diff 328 -R mbailey/voicemode
   ```
   If it substantially overlaps ours, **review and support it** rather than opening a rival. A
   second unmerged PTT PR helps nobody, and a substantive review from a user with a working
   implementation is worth more to that PR than a competing diff.
2. **Read upstream `docs/reference/control-channel.md`** and settle whether `hold` can be a new
   named intent.
3. **Comment on #312** with the design and the finding that `skip-forward` gets most of the way
   — then *ask* before building.
4. **Locally**: rebase our PTT onto 8.12.0 as a control-channel client if step 2 says that works,
   otherwise re-anchor the existing patchers. Either way the user gets PTT regardless of upstream.
