# Upstream contribution queue

Contributions to `mbailey/voicemode`, filed as `rwunsch`. Submit with
`gh auth switch --user rwunsch` first; upstream's default branch is `master`.

**Status 2026-10-01:** all nine queue items plus #536 are filed. Nothing merged, no
maintainer reply on any of it. That is upstream-wide, not about us: last release v8.12.0
(2026-07-21), and the only merge since is a CI fix (#542, 2026-09-15). PRs #537/#538 are
mergeable with no reviews. **Decision 2026-10-01: do not ping the maintainer** — wait.

No better-maintained fork exists (checked 2026-10-01, 196 forks): all have 0 stars; the only
ones ahead of master are personal branches hundreds of commits behind (williamruiz1
+54/-276 endpointing/barge-in, kmosher +30/-290 voice lab, Sallvainian +11 Bazzite support).

Evidence for every claim: `../superpowers/plans/artifacts/2026-09-05-patch-audit.md`.

## Strategy

Upstream merges roughly **5% of external PRs** (3 of the last 60 merged, the rest being
the maintainer's own agent and dependabot) — but merges them in **0–2 days** when it does,
with named credit in the changelog. Meanwhile 11 human PRs are open, the oldest since
2026-02-20, and `master` has had no public merge since 2026-07-21.

Read: the bottleneck is attention, not hostility. So —

1. **Attach to an already-open issue wherever one exists.** That is the strongest available
   signal that a maintainer will look.
2. **One fix per PR.** Never bundle. A reviewer who has to make four decisions makes none.
3. **Lead with the mechanism, not the diff.** Upstream's own commit style is diagnosis-first;
   match it.
4. **Issue before PR for anything net-new.** A large unsolicited feature PR into a repo with
   a 5-month PR backlog is a donation to the archive.
5. **Everything stays working locally regardless.** No submission blocks our own use.

## Queue

| # | Draft | Target | Type | Confidence |
|---|---|---|---|---|
| 1 | [`pr-listen-overrun.md`](pr-listen-overrun.md) | **FILED** #532 → PR #537 2026-09-06 | Bug fix | **High** — live defect, upstream comment confirms it is deliberate-but-unexamined |
| 2 | [`pr-no-silent-voice-swap.md`](pr-no-silent-voice-swap.md) | **FILED** #533 → PR #538 2026-09-06 | Behaviour fix | **High** — small, self-contained, clearly surprising behaviour |
| 3 | [`issue-service-foreign-backend.md`](issue-service-foreign-backend.md) | **FILED** #534 2026-09-06 | Bug report | **High** — reproducible, we have the failure count |
| 4 | [`comment-wslg-audio.md`](comment-wslg-audio.md) → [`comment-342-posted.md`](comment-342-posted.md) | **POSTED** #342 2026-10-01 (not #341: that is a TTS cut-off at PulseAudio `exit-idle-time`, our item (a) is recording, already #532) | Diagnosis comment | **Medium** — comment first, PR only if a maintainer engages |
| 5 | [`ptt-strategy.md`](ptt-strategy.md) | **COMMENTED** #312 2026-09-06 | Strategy | **Medium** — support the existing PR before opening a rival |
| 6 | [`issue-piper.md`](issue-piper.md) | **FILED** #535 2026-09-06 | Feature request | **Low** — zero demand signal upstream; ask, don't build |
| 7 | [`comment-kokoro-onnx-cpu.md`](comment-kokoro-onnx-cpu.md) | **POSTED** #262 2026-09-11 | Diagnosis comment | **High** — the maintainer's own closing note asks for exactly this case, by name, and we have measured it |
| 8 | [`issue-simpleaudio-blocks-windows.md`](issue-simpleaudio-blocks-windows.md) | **POSTED** #541 2026-09-12 | Bug report | **High** — proven by a resolver run, one-line fix, and the dep has broken installs in #117/#13/#319 already |
| 9 | [`comment-524-windows-corroboration.md`](comment-524-windows-corroboration.md) | **POSTED** #524 2026-09-12 | Supporting evidence | **High** — we hit both defects independently before finding the PR; supports it rather than rivalling it |

Also filed without a draft here: **#536** (2026-09-06) — `conch status` names every session
"converse"; the gap our `patch_session_name.py` closes.

## Filed 2026-10-01 (patches turned into real upstream PRs)

| Upstream | What | Replaces downstream |
|---|---|---|
| PR #552 | configurable STT request timeout (`VOICEMODE_STT_TIMEOUT[_LOCAL]`) | timeout half of `patch_simple_failover.py` |
| PR #553 | local STT servers that reject `language=auto` (speaches) get a one-time retry without it | nothing yet — new; lets a container path drop `whisper-proxy` |
| PR #556 (fixes #536) | session name in conch holder **and** queue waiter | `patch_session_name.py` env/cwd steps; the `session_names/` file lookup stays downstream |
| issue #554 → PR #555 | cancelling a `turns[]` converse doesn't stop the playback thread; `mcp.run()` can't return | see below — `patch_shutdown_abort.py` does NOT fix this |

All four: one fix each, branched from upstream master `126d15e`, tests re-run by us in a clean
venv (`env -u VIRTUAL_ENV uv run --directory <wt> ...` — our shell's `VIRTUAL_ENV` otherwise
silently runs the wrong pytest). Full suite on each branch and on clean master: 1 failure,
`test_concurrent_stdio.py::test_sounddevice_stderr_redirect_disabled`, which also fails on
unmodified master under `-n 4` (2/2 runs) and passes alone — pre-existing upstream test isolation.

**Downstream findings from this round (act on these):**
- `patch_shutdown_abort.py` (force `os._exit` after `mcp.run()`) is ineffective for the turns[]
  path: the wait is *inside* `mcp.run()` (`asyncio.Runner.close()` joins the default executor).
  Measured by the PR #555 work: 12.6s vs 13.0s linger with/without it (n=1 each); the fix
  takes it to 0.37–0.46s (n=3). Also corrects item 3 of our #342 comment — #554 says so.
- `patch_session_name.py` patches only `Conch(agent_name=...)`; `ConchQueue.register(agent=...)`
  is still "converse", so *queued* sessions are unnamed in `conch status` locally.

Deliberately **not** upstreamed: the Docker compose stack itself. See
[`why-upstream-builds-from-source.md`](why-upstream-builds-from-source.md).
