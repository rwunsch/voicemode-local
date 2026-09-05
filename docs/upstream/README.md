# Upstream contribution queue

Drafts staged for `mbailey/voicemode`. **Nothing here has been submitted.**
Submit with `gh auth switch --user rwunsch` first; upstream's default branch is `master`.

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
| 1 | [`pr-listen-overrun.md`](pr-listen-overrun.md) | new issue → PR | Bug fix | **High** — live defect, upstream comment confirms it is deliberate-but-unexamined |
| 2 | [`pr-no-silent-voice-swap.md`](pr-no-silent-voice-swap.md) | new issue → PR | Behaviour fix | **High** — small, self-contained, clearly surprising behaviour |
| 3 | [`issue-service-foreign-backend.md`](issue-service-foreign-backend.md) | new issue | Bug report | **High** — reproducible, we have the failure count |
| 4 | [`comment-wslg-audio.md`](comment-wslg-audio.md) | #341, #342 | Diagnosis comment | **Medium** — comment first, PR only if a maintainer engages |
| 5 | [`ptt-strategy.md`](ptt-strategy.md) | #312, #328 | Strategy | **Medium** — support the existing PR before opening a rival |
| 6 | [`issue-piper.md`](issue-piper.md) | new issue | Feature request | **Low** — zero demand signal upstream; ask, don't build |

Deliberately **not** upstreamed: the Docker compose stack itself. See
[`why-upstream-builds-from-source.md`](why-upstream-builds-from-source.md).
