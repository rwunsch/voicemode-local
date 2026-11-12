# Proposal: one voice supervisor, owned by voicemode-local

Status: for Robert's decision. Agreed between the voicemode-local and FELIX-voice sessions,
2026-11-12. Decision needed: approve the split, and choose the timing (route i or route ii).

## The problem

FELIX's `plugins/voicemode/scripts/voice.py` is a drifted copy of voicemode-local's
`scripts/windows/voicemode_native.py`.
- **Measured** (2026-11-12, n=1 snapshot of both files, difflib per function):
  - 46 of FELIX's 57 functions share a name with ours, holding 770 of 854 lines.
  - 16 are near-identical and 10 similar.
  - 20 have diverged, among them `setup`, `ensure_services`, `watch` and `launch`.
- Every fix to how the voice processes run therefore lands twice, Talos reviews both copies,
  and the copies drift.

The live cost (FELIX-voice, Windows, n=1, reported):
- The speech services run inside the job of whichever chat started them.
- When the Copilot app unloads that chat (seen after 12.7 min idle), every other chat loses
  speech.
- voicemode-local's supervisor already avoids this. Its services sit in a named, shared Job
  Object that outlives every connector but the last, and children leave the starting
  connector's job with `CREATE_BREAKAWAY_FROM_JOB` (read in the code). Unmeasured: whether the
  Copilot app's job allows that breakaway.

## The split

**voicemode-local owns (generic, every harness):**
1. The lifecycle of the speech services, the barge trigger and the push-to-talk listener, on
   Windows and macOS: the shared job with breakaway, leases, watchdog, status and stop.
   - It is one code path, driven by a **host profile**: roots, runtime python, service
     commands, ports, environment, policy hooks.
   - It is exposed as stable command-line entry points.
2. The shared fixes FELIX made in its copy. FELIX-voice lists them:
   - the lease is written before anything starts;
   - the watchdog starts first;
   - a port's listener is checked to be our process tree;
   - a port is never picked twice while a service loads;
   - stop signals are held until the process is recorded;
   - proxy-free loopback HTTP;
   - the repair check;
   - restarting under the runtime's python;
   - tree kill;
   - setup deadline and layout;
   - "publish only after a speech check".
3. Fixes for what FELIX works around today:
   - The conch home follows a setting instead of the home folder fixed at import. Then
     FELIX's shared-HOME trick becomes the supervisor's job.
   - A setting turns off the config search up the folder tree, replacing FELIX's stopper file.
   - Two bugs: beeps missing by default, and the pill label lagging the speaker.

**FELIX keeps (policy and glue):**
- install, runtime and repair: digest runtimes, hash locks, offline, whisper.cpp, its own
  Kokoro server, WebView2, the Mac pill venv;
- fe-bootstrap;
- building the environment, as profile values;
- preferences (`voice.barge`);
- FE-facing messages;
- the Copilot-only pieces: the `session.send` wake extension and "FELIX speaks first", neither
  of which is built yet.

`voice.py` keeps its glue and loses about 25 functions: `ensure_services`, the watchdog,
leases, ports, the tree kill, `start_helper` and `ensure_ptt`.

## FELIX's conditions, and how they are met

| # | Condition (FELIX-voice) | How |
|---|---|---|
| 1 | Vendored byte-identical at a tag, never fetched | The supervisor ships as files at a voicemode-local tag, like the patches. |
| 2 | No provider key names in vendored text | Checked 2026-11-12: `voicemode_native.py` names none. A test pins it. |
| 3 | No silent fallbacks: FELIX must see when breakaway was refused | **Change needed:** `spawn()` today quietly retries without breakaway. It will record the outcome in the run state, and the status entry point will report it. |
| 4 | Stable interface with fe-bootstrap: `active.json`, `.complete.json`, digest inputs, `install-state.json`, `last-start.json`, exit 75 | Those files stay FELIX-written. The supervisor reads what the profile names and never rewrites FELIX's files. |
| 5 | FE-facing text comes from FELIX | The supervisor returns structured results (code, detail). FELIX words them, through a profile hook. |
| 6 | No console windows, stdio passes through, FELIX ports 18880/12022 plus `ports.json` | Already no console window (`CREATE_NO_WINDOW`). Port ranges become a profile value; ours stay separate. |
| 7 | No ffmpeg, hash-locked, offline after setup, loopback only, proxies stripped | The supervisor downloads nothing in FELIX's profile, binds loopback only, and adopts FELIX's proxy-free loopback HTTP. |

## Timing (Robert's call)

#1743 (FELIX voice) is meant for main soon. The bug above breaks multi-chat voice about 12
minutes after an idle chat is unloaded.

- **Route i (recommended by both sessions):** #1743 ships now with a small, separate fix for
  that bug, and the migration follows as its own PR.
  - The fix starts the services and helpers outside the chat's job, via `Win32_Process.Create`.
    That is proven in FELIX's installer, so it doesn't depend on the unmeasured breakaway.
  - The migration then replaces it with the shared supervisor, which must be reviewed on its
    own.
- **Route ii:** hold #1743 until the migration is done. That's slower, and voice on main stays
  without the fix for longer.

## Order of work after approval

1. FELIX-voice: the small fix for #1743 (route i). It also measures whether the Copilot app's
   job allows breakaway (one spawn, then `IsProcessInJob`), which decides the supervisor's
   Windows path.
2. voicemode-local, test-first:
   - the host profile and the entry points;
   - the shared fixes from FELIX's copy;
   - the recorded breakaway outcome (condition 3);
   - parity tests built from FELIX's ~90 voice tests;
   - tag a release.
3. FELIX-voice: `voice.py` becomes a thin wrapper over those entry points, in one PR, with
   Talos review.
4. voicemode-local: the conch home setting, the walk-up setting, beeps, the pill label lag.

## Not known yet

- Whether the Copilot app's job allows breakaway (decides the Windows path; measured in step 1).
- Whether Copilot app requests carry a progress token (the CLI's do; the app's are unmeasured).
- The cause of the missing beeps (both sessions, unexplained).
